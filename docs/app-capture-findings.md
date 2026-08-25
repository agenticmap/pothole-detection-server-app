---
updated: 2026-08-23
---

# What 2916 real frames say about the capture side

Findings from analysing the first two collection drives, gathered while building Phase 2.7
([`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md)). **None of these are
server bugs.** They are capture-side, they explain most of the detection difficulty, and several
are cheap to fix.

Every number below came from SQL against `pothole_db` and is reproducible with the queries shown.
Every cause is a specific line in the app repo
(`C:\Users\satta\Desktop\Projects\pothole-detection-mobile-app`). The app-facing version of this
document is `docs/server-data-findings.md` in that repo.

**Dataset under discussion:** 2916 frames and 2728 observations, 2026-08-16 → 2026-08-23, north
Toronto, two devices — 2889 frames / 2630 observations from one and 27 / 98 from the other.

---

## F1 — Wire timestamps are whole-second, and it quietly degrades fusion

**Cause.** `network/PotholeApi.java` had one `iso8601()` helper, used by both the events path
(`:80`) and the frames path (`:153`):

```java
new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)   // no milliseconds
```

**Measured.** Every timestamp on both tables is a whole second, and 2916 frames collapse onto 1226
distinct instants (up to 5 frames share one):

```sql
SELECT count(*) frames,
       count(*) FILTER (WHERE date_trunc('second', ts_utc) = ts_utc) whole_second,
       count(DISTINCT ts_utc) distinct_ts
FROM asset_frame;
--  2916 | 2916 | 1226      (observations: 2728 | 2728)
```

**Why it matters.** Fusion pairs a frame to an observation inside `|Δt| < 3000 ms` and breaks ties
with `ROW_NUMBER() ... ORDER BY abs(Δt), ST_Distance(...)` (`app/fusion/service.py:163-168`). With
second resolution, Δt can only take three values — and does:

```sql
SELECT abs(delta_ms), count(*) FROM fusion_pair GROUP BY 1 ORDER BY 1;
--  0 → 845     1000 → 793     2000 → 204
```

The distance tie-break does not rescue it, because a burst of frames shares one GPS fix:
`delta_m = 0` for **1198 of 1842** pairs. So for a large share of pairs, *both* ordering keys are
ties and the "nearest observation" is effectively arbitrary. Every `fused_confidence` downstream
inherits that.

> **Partly superseded by Phase 2.2d, and the residue is smaller.** The ranking is no longer
> `abs(Δt)` first: it is a lookahead cost in which Δt enters only through the kinematic residual
> `|Δt + Δd/speed|`, so a quantised Δt shifts a candidate's score rather than deciding the order
> outright, and `delta_m = 0` is now *penalised* (it is outside the lead band) instead of winning the
> tie-break. What remains is that quantisation still adds up to ±500 ms of noise to that residual.
> This finding is still worth the app-side fix — which shipped — but it is no longer the dominant
> source of arbitrary pairings. See
> [`phase-2.2d-pairing-search.md`](./phase-2.2d-pairing-search.md).

**Fix — done.** `PotholeApi.iso8601` now formats `.SSS`; Room already held epoch millis
(`tsUtcMs`), so the precision existed and was being discarded only at serialization. Pinned by
`PotholeApiTest` (5 tests). **No server change was needed:** `FrameMetadata` accepts
`...T12:56:38Z`, `...38.123Z` and `...38.123+00:00`, and `asset_frame.ts_utc` is `timestamptz`
with microsecond precision, so milliseconds round-trip exactly.

---

## F2 — GPS is coarser than capture

**Measured.** 2916 frames carry only **1010 distinct geometries** — about three frames per fix:

```sql
SELECT count(*) frames, count(DISTINCT geom::text) distinct_geoms FROM asset_frame;
--  2916 | 1010
```

De-duplicated cadence is a median 1 s gap (p90 3 s), with 2–5 frames inside one second.

**Why it matters.** Beyond F1's tie-break problem, `asset_cluster.centroid` is a
confidence-weighted mean of member points, so a repeated fix is counted repeatedly and pulls the
centroid toward wherever the phone happened to be holding a stale location.

**Recommendation.** Attach the location at frame-capture time rather than reusing the last fix, or
interpolate between fixes using the frame's own timestamp (which F1 now makes usable). Failing
that, the server could dedupe on `(device_id, geom, ts_utc)` — but fixing it at source is better,
because the server cannot recover information the phone never sent.

---

## F3 — A quarter of the model's input is black bars, and most of the rest is not road

**Cause, in two steps.** `vision/MapsCameraBinder.kt:286-299` asks for a square `Size(640, 640)`
under `RATIO_4_3_FALLBACK_AUTO_STRATEGY`. No camera offers 640×640 at 4:3, so the closest-lower 4:3
size wins: sensor **640×480**. Then `setOutputImageRotationEnabled(true)` (`:285`) plus the
portrait activity lock (`AndroidManifest.xml:49`) applies a quarter turn, swapping the sides to
**480×640**. The code comment at `:286-288` says this outcome was observed but "relying on that was
luck".

`vision/RoadGateModel.kt:177-201` then letterboxes 480×640 into the model's 640×640:
`scale = min(640/480, 640/640) = 1.0`, `padX = 80`, `padY = 0`. So the frame is **not resized at
all** — it is centred with 80-pixel black bars either side. **25% of every inference is spent on
padding the app wrote itself**, and of the remaining 75%, the actual road band is roughly a third:
sky and trees fill the top half, the hood the bottom ~15%.

Nothing anywhere crops to a road region. The uploaded JPEG is the full analysis frame at quality 70
(`PotholeFrameAnalyzer.kt:210-220`), which is why sizes average ~27 KB (min 10, max 71) — *below*
the app's own documented expectation of 40–70 KB, consistent with a lot of low-detail sky, hood and
darkness.

**This class of bug has bitten once already.** `docs/performance-optimization-changes.md:62-67` in
the app repo records a 90°-rotated frame with "sky filling half the frame" producing 27 stored
frames at 0.26–0.50 and only 3 above the 0.55 confirm threshold. Same failure, different cause:
that was rotation, this is mount geometry plus full-frame capture.

**Partly mitigated server-side.** Phase 2.7 added an ROI crop before letterboxing
(`DETECTION_ROI_ENABLED`, default 0.45–0.90 of frame height), which puts **1.78× more road pixels**
into the tensor — 245,760 against 138,240, measured. But the server can only crop what it was
sent; the app is throwing away resolution before upload.

**Recommendation.** Either capture landscape for the analysis stream (the sensor's native
orientation, no rotation, no side bars), or select a road ROI on-device before inference and upload
that. Landscape is the bigger win and removes the padding entirely.

---

## F4 — There is no night, low-light or thermal gating at all

**Cause.** No `Camera2Interop`, no AE/exposure compensation, no lighting or time-of-day gate
anywhere in the camera path. `PausedReason` has exactly three values — `STARTING`, `STATIONARY`,
`NO_FIX`. `res/values/strings.xml:66` defines `camera_thermal_paused` ("Paused: phone is in
power-save or thermal-throttled state") and **nothing references it**: thermal pausing was never
wired up.

**Measured.** **2000 of 2916 frames** — 69% of everything collected — come from a single 20:00 local
hour. And night driving is much faster:

```sql
SELECT CASE WHEN extract(hour from ts_utc - interval '4 hours') BETWEEN 6 AND 19
            THEN 'day' ELSE 'night' END tod,
       count(*) total, count(*) FILTER (WHERE sensor_class='pothole') potholes,
       round(avg(speed_mps)::numeric,1) avg_speed
FROM asset_observation GROUP BY 1;
--  day   | 2062 | 139 | 11.7 m/s  (42 km/h)
--  night |  666 |   1 | 20.0 m/s  (72 km/h)
```

**One** night pothole classification against 139 by day. The single highest-scoring frame in the
whole collection (device p = 0.811) is rain on the windshield under headlight glare, with no road
surface visible.

**Why it matters.** At 72 km/h a frame covers 20 m of road, and motion blur at night on a phone
sensor is severe. These frames cost storage, upload bandwidth, inference time and labelling
attention, and cannot support a detection decision. They also drag every aggregate metric down in a
way that looks like model weakness.

**Recommendation.** Gate camera capture on a speed *ceiling* as well as the existing floor
(currently 5 km/h, `Prefs.java:42`), and add a brightness or time-of-day gate — the mean luma of
the already-decoded RGBA frame is nearly free to compute. Then wire the thermal string that is
already written. The sensor pipeline should keep running at night; it is the camera that has
nothing to see.

---

## F5 — Record-time tuning never reaches the server, and it has already caused ambiguity

**Measured.** Minimum `device_probability` per day:

```sql
SELECT date_trunc('day', ts_utc)::date, count(*),
       round(min(device_probability)::numeric,4), round(max(device_probability)::numeric,4)
FROM asset_frame WHERE device_probability IS NOT NULL GROUP BY 1 ORDER BY 1;
--  2026-08-18 |   20 | 0.2592 | 0.6551
--  2026-08-19 |   27 | 0.0000 | 0.0000
--  2026-08-22 |  869 | 0.0501 | 0.7729
--  2026-08-23 | 2000 | 0.0501 | 0.8110
```

A hard floor at 0.2592 on 08-18 against **exactly 0.0501** on both big drives. The camera
confidence floor was evidently lowered from the 25% default to about 5% — and I could only work
that out by *inferring it from the distribution*, because none of it is uploaded.

**Cause.** The app records this properly, per segment, in `survey_session` — `sigma0`, `k_window`,
`k_baseline`, `min_speed_mps`, `camera_min_speed_mps`, `camera_conf_floor`, `camera_confirm`,
`handling_filter`, plus `model_id`, `app_version`, `device_model` — and writes it to
`sessions.geojson` on export. `PotholeApi` posts none of it. The app's own
`SurveySession.java:26-32` explains why the session row is the authority (the export manifest reads
live prefs and so mislabels a drive taken at an older threshold), and then that authority stays on
the phone.

**Why it matters.** Two drives taken at different thresholds are pooled indistinguishably. Any
comparison across drives, any per-drive precision figure, and any attempt to re-tune the sensor
model is confounded by a parameter the server cannot see.

---

## F6 — No denominator — which is also why there are no training negatives

**Cause.** `session_client_id` is stored on both `event` and `pending_frame`, is written into
`events.csv` and `frames.csv` (`DriveExporter.java:163, 197`), and is **never posted**.

**Why it matters, twice over.**

1. **Coverage.** The server cannot distinguish "this street was surveyed and found clean" from
   "never surveyed". That is the difference between a defensible municipal report and a dot map.
2. **Negatives.** The training archive contains **zero background images** — all 5322 contain a
   pothole (see [`model-attribution.md`](./model-attribution.md)) — so the model has never been
   shown what a *non*-pothole road looks like. That is the most likely cause of the false positives
   found on real frames: manhole covers, painted crosswalk markings, rain on glass. Surveyed-and-
   clean frames **are** the negatives, and the app is the only thing that knows which frames those
   are.

**The privacy tradeoff, stated rather than glossed.** The app deliberately withholds tracks (its
hard invariant #10) because a track is a route reconstruction, citing the server's decision to
reject a stable device pseudonym. That reasoning is sound for `survey_point` — a polyline records
where someone drove even where nothing was detected. It is weaker for the session *id*: the server
already stores a stable `device_id` UUID alongside every geometry, so detections already permit
partial reconstruction. What the server protects is the API boundary, where
`cluster_detail_service.py:67` uses `dense_rank()` so `device_id` never leaves Postgres.

So the recommendation is narrow: **upload the session id, its tuning snapshot, and coverage
aggregates (`distance_m`, `point_count`, duration, `used_sensor`/`used_camera`) — not the
breadcrumb polyline.** That buys the denominator and the negatives without handing over a route.
Keep the polyline device-local, or behind the staff tier as the app's roadmap already proposes.
This is a judgement call about a real tradeoff and should be reviewed as one.

---

## F7 — The uploaded frame set is selection-biased

A frame is stored only if its best box clears the record-time confidence floor
(`PotholeFrameAnalyzer.kt:193-198` returns `DROP` otherwise; `MapsCameraBinder.kt:324` discards it).
At the ~0.05 floor used for the two big drives the bias is mild — almost any frame with any weak
detection was kept, which is why the median `device_probability` is 0.118. At the 0.25 default it
would be severe.

**Consequence to carry forward.** Phase 2.7's labelled evaluation set is drawn from an
already-filtered population, on top of being deliberately enriched by stratified sampling. Precision
at a threshold remains comparable between models; the positive *rate* is not a prevalence estimate
for Toronto roads, and should never be quoted as one.

---

## F8 — Three loose ends, reported rather than resolved

- **27 frames with probability exactly 0.0000 and NULL detections** (2026-08-19, the second
  device). Those should have been `DROP` and never persisted. The likeliest explanation is an older
  app build, but I did not confirm it and am not asserting it.
- **59 orphaned JPEGs** under `storage/frames` with no `asset_frame` row.
- **`schema_version` is missing from frame multipart metadata** while present on events — a real
  asymmetry in the wire contract, worth closing whenever frames metadata is next touched.

---

## Recommendations, ranked by effect ÷ effort

| # | Change | Effort | Effect |
|---|---|---|---|
| 1 | **Millisecond timestamps** (F1) | one line — **done** | Restores fusion's time tie-break from 3 values to continuous |
| 2 | **Speed ceiling + brightness gate on camera capture** (F4) | small | Stops collecting the 69% of frames that cannot support a decision |
| 3 | **Post `session_client_id` + tuning + coverage** (F5, F6) | medium; needs a server table | Unlocks coverage reporting *and* mined negatives, the likeliest accuracy win |
| 4 | **Landscape analysis stream, or on-device road ROI** (F3) | medium | Removes 25% padding and most of the sky from every inference |
| 5 | **Per-frame location** (F2) | small–medium | Fixes the distance tie-break and centroid weighting |
| 6 | **Wire up `camera_thermal_paused`** (F4) | small | The string already exists; sustained capture currently has no thermal backstop |
| 7 | **`schema_version` on frame metadata** (F8) | trivial | Contract symmetry |

Nothing here requires a model, and items 1–2 alone would make the next drive materially more
useful than the last two.

## Recommended next phase

**Phase 2.8 — Capture quality and provenance**, covering items 2–7 plus the server side of item 3
(an `asset_survey_session` table, coverage on the dashboard, and a negatives-mining query for
training). Rationale: no detector rescues 480×640 night-rain frames taken at 72 km/h, and fusion's
Δt currently has three possible values. Fixing the source is worth more than any amount of
downstream cleverness.

Then **Phase 2.9 — the VLM verifier** (`hybrid_v1.py` is already built and crop-tested), once
Stage 1 has been measured and `VLM_VERIFY_LOW`/`HIGH` can be set from a real gray-zone
distribution instead of guessed.

Two things sit outside that sequence and should not be forgotten: **Phase 2.6's remaining
hardening** (shared rate limiter, per-IP limits, frame GC, storage budget, TLS, shadow-ban) gates
any pilot and `storage/` is currently unbounded; and a **second device** is the only way to see a
real cluster at all, since `CLUSTER_MIN_DISTINCT_DEVICES=2` and 2889 of 2916 frames came from one
phone.
