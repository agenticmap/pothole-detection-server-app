---
updated: 2026-08-31
---

# Why nothing corroborates: route coverage, not the detector

**Headline.** The project's standing negative result — *pothole detections are not
spatially reproducible* — is wrong. So is the first attempt to retract it. The real
explanation is that **91.9 % of pothole detections sit on road that only one day ever
covered**, so they could not co-locate across days no matter how good the detector is.
Conditioned on road that was actually revisited, the whole dataset holds **18** pothole
detections, and zero cross-day co-locations is precisely what chance predicts there
(null median 0, p ≈ 0.64).

Reproduce everything below with `scripts/session_regimes.py --coverage` (read-only).

This document is written to be read on its own. It records two wrong analyses as well
as the right one, because both reached print, and the reason they did is more useful
than the conclusion.

---

## 1. The claim

`crowd_sweep.py --reclassify` measured, over 4,637 observations from 6 collection days:

| | potholes | cross-day co-located within 25 m |
|---|---|---|
| observed | 243 | **0** |
| day-matched null, 30 draws | 243 | 10–35, median 20 |

Zero, against a null that never once went below 10. Recorded in
[`paper-fidelity-assessment.md`](./paper-fidelity-assessment.md) §4c as *"the pothole
class is significantly anti-co-located"*, and in
[`integration-round-2026-08.md`](../phases/integration-round-2026-08.md) as the finding
that blocked the whole crowdsourcing goal.

The null was not naive. It matched the **total count** and the **per-day counts**, which
controls for the two obvious confounds — a class too sparse to co-locate, and a class
concentrated on a single day. That is why the result was believed.

## 2. Wrong explanation #1: "instrument regimes"

Deriving sessions from a 20-minute gap gives 12 sessions across 2 devices. They appear
to split cleanly on `gbar_in_max / accel_max_g` — window energy over peak acceleration:

| band | sessions | median `gbar/g` | pothole rate |
|---|---|---|---|
| low | 9 | 1.75 – 3.48 | 0.0 – 4.7 % |
| high | 3 | 9.91 – 19.05 | 20.4 – 24.2 % |

No overlap, nothing in the corridor between. Raw `accel_max_g` (1.64–3.40) and
`accel_std` (0.45–0.74) span the *same* range in both bands, so the story wrote itself:
the roads and forces were comparable and only the app's derived features differed, so
the pooled null was mixing two instruments. Restricting to the low band, the same zero
became ordinary (p ≈ 0.49). Published as a retraction.

### Why it is wrong

**`gbar` is the classifier's dominant input.** Selecting low-`gbar` sessions is very
nearly selecting sessions with few potholes, so the follow-on conclusion — "too few
potholes for the test to have power" — was manufactured by the selection itself. The
stratification variable and the outcome are the same quantity.

Two measurements confirm it rather than merely arguing it:

**`gbar/g` is not a property of a session.** Every session is a mixture of the same two
event types. The low tails are near-identical; only the mixture fraction moves, and that
fraction *is* the pothole rate:

| session | p10 | p50 | p90 | fraction > 6 |
|---|---|---|---|---|
| 4eb6:7 | 1.12 | 12.57 | 26.43 | 67.9 % |
| 4eb6:2 | 0.86 | 2.79 | 22.91 | 33.7 % |
| 4eb6:5 | 1.08 | 3.49 | 11.41 | 38.5 % |
| 4eb6:9 | 0.84 | 2.13 | 10.04 | 14.9 % |

**Restricted to ground both bands drove, the ordering reverses.** In grid cells visited
by both (~100 m cells, 481 observations), the "high" band measures 2.60 and the "low"
band 7.63 — backwards. A genuine instrument state does not flip depending on which road
you look at. Individual sessions swing just as hard: `4eb6:7` is 12.57 session-wide but
2.16 in shared cells; `4eb6:2` is 2.79 session-wide but 10.60 there.

`--quarantine` still reproduces this, labelled as a wrong turn.

## 3. The clean control, which left the finding standing

Stratifying by **device** is legitimate — which phone recorded a point is not downstream
of the classifier. Doing so, the original finding survived:

| device 4eb6 only, 4,539 obs, 6 days, 11 sessions | n | observed | null | draws ≤ obs |
|---|---|---|---|---|
| pothole | 223 | **0** | 2–27, median 11 | **0 / 200** |
| `crack` | 2,990 | 846 | 705–849, median 787 | 199 / 200 |
| `not` | 1,326 | 262 | 205–303, median 256 | 129 / 200 |

One phone, one instrument, still zero against a null of 2–27. So the effect was real and
needed a different explanation — not a different slice.

## 4. The actual explanation: route coverage

The question neither analysis had asked: **how many distinct days actually drove within
25 m of each detection?** This is computed over *all* observations of any class, so it
describes where the driver went, not what the classifier decided — the independence
`--quarantine` lacked.

| device 4eb6 | n | mean days covering | on road only one day covered |
|---|---|---|---|
| pothole-classed | 223 | 1.09 | **91.9 %** |
| everything else | 4,316 | 1.40 | 68.4 % |
| all observations | 4,539 | 1.38 | 69.5 % |

**Nine in ten pothole detections are on road that was never revisited.** For those, a
cross-day repeat is geometrically impossible.

That also explains why the null exceeded the observed value rather than merely matching
it. The null drew from all of that day's observations — only 68.4 % single-visit — so
random draws landed on revisited road far more often than the real detections could.
The null was measuring a population with systematically more opportunity. The "anti-
co-location" was that gap and nothing else.

### The conditioned test

Restrict both the detections **and** the draw pool to road that at least two days
actually covered — 1,384 observations, 30.5 % of the data:

| class | n | observed | null | median | draws ≤ obs |
|---|---|---|---|---|---|
| pothole | **18** | 0 | 0–7 | **0** | 256 / 400 (p ≈ 0.64) |
| `crack` | 953 | 846 | 831–886 | 862 | 36 / 400 |
| `not` | 413 | 262 | 223–285 | 255 | 297 / 400 |

The null's own median is zero. Eighteen detections spread over the revisited fraction of
six days are expected to produce no cross-day repeats at all. **There is nothing left to
explain.**

## 5. What this corrects

**The corroboration failure is route coverage.** Not the detector, not classification,
not clustering geometry, and not instrumentation. The pipeline produced zero corroborated
defects because the collection never drove the same road twice where the detections were.

**A repeated factual error goes with it.** The rate swing was described throughout as
happening *"across the same roads"* — the sentence that made per-drive calibration look
like the culprit. They were **not** the same roads: 69.5 % of all observations are on
road only one day covered, so the days largely drove different routes. Since `gbar/g`
varies strongly by location (§2), different routes is now the leading explanation for the
0.2 % → 24.2 % swing, displacing the "per-session tuning the app never uploads" story.
That does not make the missing provenance harmless — it makes it unproven as a cause.

**One earlier finding is untouched**, because it never depended on any of this: device
`a1878f6d` puts 94.9 % of its `time_in_max` values off the 0.033548 s (29.81 Hz) grid
that carries **100 %** of the other phone's 4,539 observations. It samples roughly 8×
faster, which inflates every window-summed feature. Only 98 observations, and excluded
from every test in §3 and §4 — but two phones reporting incomparable window features
matters before a second device is added.

## 6. What to collect

**A fixed loop, driven N times, one phone, unchanged mount.** This is not a preference;
it is the only design under which the question is answerable. Today 8 % of pothole
detections are eligible for corroboration. On a repeated loop it is 100 % by
construction, and the paper's accuracy-versus-survey-count curve (its §5.5) becomes
measurable for the first time.

Rough sizing from §4: the revisited 30.5 % of the current data yielded 18 eligible
detections across 6 days. Corroboration needs detections to *meet*, so the useful
quantity is eligible detections per unit of repeated road — and a short loop maximises
it far faster than more kilometres do.

**And a standing methodological rule.** Any future co-location claim must condition its
null on coverage. Matching per-day counts is not enough; it was not enough here, and it
produced a confident, significant, wrong answer that stood for two days.

## 7. How the errors got through

Worth naming, because the fix is structural rather than a matter of care.

Both wrong analyses came from **nulls that controlled for everything except the thing
that mattered**. The first controlled for count and per-day distribution but not
coverage. The second controlled for coverage implicitly — by restricting to a band — but
stratified on the outcome variable, which is worse than not controlling at all.

Neither error was visible from the number. Both produced a plausible p-value and a
plausible story. What exposed each one was a *control the code did not have*: for §2,
comparing bands on ground they both drove; for §1, asking how many days had been near
each point. Both are now modes in `scripts/session_regimes.py` — `--quarantine` kept
deliberately as the reproducible wrong turn, `--coverage` as the test that should have
run first.
