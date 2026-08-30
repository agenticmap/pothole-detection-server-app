---
updated: 2026-08-24
---

# Runbook — the pairing search

Procedure only. For *why* the search is shaped this way, read
[`phase-2.2d-pairing-search.md`](../phases/phase-2.2d-pairing-search.md).

Every command runs **from the repo root**, and every one of them needs `DATABASE_URL` pointing at
the database you mean. There is no interactive confirmation on most of these.

**Nothing here is destructive to collected data.** `fusion_pair` is derived entirely from
`asset_frame` + `asset_observation`, and none of these steps writes to either. The worst outcome of
a wrong knob is a `fusion_pair` table you have to regenerate, which is step 3.

---

## Before anything: three things not to do

| Don't | Because |
|---|---|
| Point `pytest` at `pothole_db` | The fixtures `TRUNCATE` every table. `conftest.py` refuses anything but `pothole_test`/`pothole_ci` — do not disable that guard. Your 2728 observations and 2916 frames are unrecoverable. |
| Set `FUSION_FRAME_ONLY_ENABLED=true` yet | `server_probability` is NULL on every frame because no model exists, so the only input is the on-device probability, whose confidence floor was lowered to ~5% mid-collection (p50 **0.118**). You would fill clustering with sub-threshold guesses. Wait for Phase 2.7 to measure a threshold. |
| Change a `FUSION_*` pairing knob without step 3 | The pairing search only re-decides a frame's partner when that frame goes back through the job. Skip the requeue and the config applies to new frames only, leaving `fusion_pair` a mixture of two rankings with nothing marking which row is which. |

---

## Step 0 — Apply migration 012 (~5 s)

Migrations run automatically at app startup when `ENV=development`, so if the container has been
restarted since 2.2d landed, this is already done. To check:

```bash
docker exec pothole-postgres psql -U pothole -d pothole_db -c \
  "SELECT filename, applied_at FROM schema_migrations ORDER BY filename DESC LIMIT 2;"
```

You want to see `012_pairing_search.sql`. If it is missing, **restart the app container** — that is
the clean path, because `run_migrations` records the file and its checksum in `schema_migrations`.

To apply it without a restart (note the `-i`, or `psql` gets no stdin):

```bash
docker exec -i pothole-postgres psql -U pothole -d pothole_db \
  < migrations/012_pairing_search.sql
```

That route leaves no `schema_migrations` row, so the app applies it again on its next startup.
Harmless, since every statement is `IF NOT EXISTS` — but it does leave the ledger briefly out of
step with the schema, which is the thing the ledger exists to prevent. Prefer the restart.

The migration is additive and idempotent, and backfills nothing. Every
existing pair keeps `is_primary = false` and `match_cost = NULL`, and the member gate's `COALESCE`
falls back to the old `max()` for them — so applying it changes no behaviour on its own.

---

## Step 1 — Measure before you change anything (~30 s)

```bash
python scripts/pairing_eval.py --diff
```

Read-only. This prints, per speed bucket and sensor class, how many frames the lookahead cost would
assign differently from the old ranking. On `pothole_db` at the shipped defaults:

```
TOTAL                            2302      850  36.9%
```

Two columns matter beyond the headline:

- **`cost@old`** — the lookahead cost of the frame the *old* ranking chose. A high value (the real
  data shows 4.1–4.8) means the old ranking was picking frames well outside the lead band, i.e.
  frames taken where the camera could not have seen the pothole. Near zero would mean the two
  rankings mostly agree and this phase buys you little.
- **`mean dt`** — should be *negative* if the lookahead model fits your capture setup. Positive
  means frames are being matched to events that fired before the photo, which points at a clock
  offset between the camera and sensor writers rather than at the search.

To compare against the pre-2.2d window as well as the pre-2.2d ranking, pass `--window-ms-max 3000`
(this reproduces the 713 / 2197 / 32.5% figure quoted in the design doc).

---

## Step 2 — Sanity-check the knobs you are about to apply

```bash
python -c "from app.config import settings; print(
  settings.fusion_pairing_cost_enabled,
  settings.fusion_lead_near_m, settings.fusion_lead_far_m,
  settings.fusion_window_m, settings.fusion_window_ms_max)"
```

The two that matter most:

- **`FUSION_WINDOW_M` must comfortably exceed `FUSION_LEAD_FAR_M`.** The spatial gate runs *before*
  the cost, so a lead band wider than the search window is a band the search can never reach. At
  the defaults, 40 > 30 with 10 m of slack for GPS error.
- **`FUSION_LEAD_NEAR_M` / `FUSION_LEAD_FAR_M` are 5.0 / 30.0 and that is a hypothesis, not a
  measurement.** The band is a property of your lens and mount pitch. Step 5 replaces it.

---

## Step 3 — Re-fuse (~1 min for 2916 frames)

```bash
python scripts/requeue_frames.py --dry-run   # look first
python scripts/requeue_frames.py            # then do it
```

This clears `processed_at` and drains the queue in batches of `FUSION_BATCH_SIZE` (500), printing a
before/after summary. It scores nothing — unlike `scripts/backfill_detection.py`, which also clears
`processed_at` but requires `--model` because its job is to run the detector first.

Measured on `pothole_db`, 2916 frames over 6 batches:

| | before | after |
|---|---|---|
| pairs | 1842 | 2158 |
| distinct observations paired | 472 | 476 |
| primaries | 0 | 476 |
| mean `delta_m` | 5.25 m | **15.29 m** |
| mean `delta_ms` | −92.8 | **−536.6** |

**The two bottom rows are the acceptance test.** Mean separation should move from near zero into the
middle of your lead band, and mean `delta_ms` should go decisively negative. If separation stays
near zero, the cost is not being applied — check `FUSION_PAIRING_COST_ENABLED` and that
`match_cost IS NOT NULL` on the new rows.

If the drain reports `no progress`, that is the retry grace working as intended, not a failure:
unpaired frames younger than `FUSION_RETRY_GRACE_MINUTES` (30) are deliberately held back so a
late-uploading event can still pair with them. They drain on a later tick.

### Expect the member pool to shrink

This surprises people, so it is worth stating plainly. On `pothole_db` the clusterable pool went
**11 → 4**, and the composition changed:

| | before | after |
|---|---|---|
| `not`-classed | 7 | 0 |
| `crack`-classed | 1 | 0 |
| pothole, outlier-flagged | 2 | 3 |
| pothole, clean | 1 | 1 |

That is an improvement, not a regression. Attaching camera verdicts to kinematically implausible
partners is what was pushing crack- and `not`-classed observations over the `>= 0.5` member floor,
through the member gate's `OR max_fused >= $2` branch that bypasses both the class filter and the
outlier gate. Better pairing stopped feeding that hole. The hole itself is still open.

---

## Step 4 — Re-cluster

Clustering runs every `CLUSTERING_INTERVAL_MINUTES` (15), so you can simply wait. To force it:

```bash
python -c "
import asyncio
from app.database import create_pool
from app.fusion.service import run_cluster_job
async def m():
    p = await create_pool()
    print('clusters upserted:', await run_cluster_job(p))
    await p.close()
asyncio.run(m())"
```

**Expect 0 clusters on the current data, and do not read that as a failure of this phase.** With 4
members and `CLUSTER_MIN_POINTS=3`, a cluster needs three of them within 25 m of each other and they
are not. The blockers are the outlier gate (139 of 140 potholes flagged — see the investigation
section of the design doc) and a single substantive device, neither of which the pairing search
touches.

---

## Step 5 — Replace the guessed lead band with a measured one

This is the step that turns the default from a hypothesis into a finding. It needs Phase 2.7's frame
labels, so it is blocked until those exist.

```bash
python scripts/label_frames.py            # Phase 2.7, ~300 frames
python scripts/pairing_eval.py --fit-lead
```

`--fit-lead` reports the `delta_m` distribution for frames a human confirmed contain a pothole and
prints a `FUSION_LEAD_NEAR_M` / `FUSION_LEAD_FAR_M` pair to paste into `.env`. Below **30** confirmed
frames it refuses and prints nothing, deliberately — percentiles from a handful of rows are noise,
and printing them invites someone to treat them as a measurement. Today it says:

```
Only 0 frame(s) are labelled as containing a pothole; 30 is the floor for fitting a band.
```

It fits the band from the *nearest-in-time* candidate, not the lowest-cost one. That is on purpose:
fitting the band with the cost the band parameterises would be circular.

After changing the band, **go back to step 3** — the new band only reaches existing pairs through a
requeue.

---

## Rolling back

```bash
# .env
FUSION_PAIRING_COST_ENABLED=false
FUSION_WINDOW_M=25
FUSION_WINDOW_MS_MAX=3000
```

then step 3 again.

All three lines are needed. The flag restores the pre-2.2d **ranking**, but not the pre-2.2d
windows: `FUSION_WINDOW_MS` no longer exists, and leaving `FUSION_WINDOW_M` at 40 keeps the wider
search even under the old ordering.

The schema does not need reverting. `match_cost` goes back to NULL on re-fused rows and `is_primary`
keeps working — it is derived from `match_cost` with `NULLS LAST`, so under the legacy ranking every
pair is uncosted and the tie-break falls to `frame_client_id`. The member gate's `COALESCE` then
behaves as it did before 2.2d.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `column "is_primary" does not exist` | Migration 012 not applied — step 0. The app container applies migrations only at startup, so a container started before 2.2d landed will not have it. |
| `--diff` prints "No candidate pairs at all" | Frames and observations share no `device_id`, or do not overlap in time. The join is same-device by construction; check `SELECT DISTINCT device_id` on both tables. |
| Pairs unchanged after a requeue | `FUSION_PAIRING_COST_ENABLED=false`, or the `.env` was not reloaded. Confirm with step 2, and check `count(*) FILTER (WHERE match_cost IS NOT NULL)`. |
| `fusion_pair` row count grew a lot and events look duplicated | You are on a build without `_DELETE_PAIRS_FOR_FRAMES_SQL`. Re-fusing without it leaves the old row for every reassigned frame, under the old `event_client_id`. Truncate `fusion_pair` and requeue. |
| Unpaired frames never retire | Working as designed for 30 minutes (`FUSION_RETRY_GRACE_MINUTES`). Set it to 0 for the pre-2.2d behaviour of retiring the whole batch. |
| `duplicate key value violates unique constraint "idx_fusion_pair_primary"` | Should be impossible — the job demotes before it promotes, in one transaction. If you see it, something is writing `is_primary` outside `run_fusion_job`. |

### Useful queries

```sql
-- Did the new ranking actually apply?
SELECT count(*) AS pairs,
       count(*) FILTER (WHERE match_cost IS NOT NULL) AS costed,
       count(*) FILTER (WHERE is_primary) AS primaries,
       count(DISTINCT event_client_id) AS observations
FROM fusion_pair;

-- The acceptance test: separation inside the lead band, time offset negative.
SELECT round(avg(delta_m)::numeric, 2)  AS mean_delta_m,
       round(avg(delta_ms)::numeric, 1) AS mean_delta_ms,
       count(*) FILTER (WHERE delta_ms > 0) AS forward_picks
FROM fusion_pair;

-- Cost distribution. A p50 far above 1.0 means most picks sit outside the band,
-- which is a sign the band itself is wrong for this camera -- see step 5.
SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY match_cost)::numeric, 3) AS p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY match_cost)::numeric, 3) AS p95,
       count(*) FILTER (WHERE match_cost < 1.0) AS inside_band
FROM fusion_pair;

-- Who is actually clusterable, and by which branch of the member gate.
WITH p AS (
  SELECT event_client_id,
         COALESCE(max(fused_confidence) FILTER (WHERE is_primary),
                  max(fused_confidence)) AS mf
  FROM fusion_pair GROUP BY 1)
SELECT o.sensor_class, o.sensor_is_outlier,
       (o.sensor_class = 'pothole' AND o.sensor_is_outlier IS NOT TRUE) AS via_class,
       count(*)
FROM asset_observation o LEFT JOIN p ON p.event_client_id = o.client_id
WHERE o.received_at > now() - make_interval(days => 30)
  AND ((o.sensor_class = 'pothole' AND o.sensor_is_outlier IS NOT TRUE)
       OR COALESCE(p.mf, 0.0) >= 0.5)
GROUP BY 1, 2, 3 ORDER BY 4 DESC;

-- Every clustering run's verdict. outputs_count = 0 with inputs_count > 0 means
-- the members exist but are too scattered for CLUSTER_MIN_POINTS.
SELECT started_at, inputs_count, outputs_count, params_jsonb->>'eps_m' AS eps_m
FROM cluster_run ORDER BY started_at DESC LIMIT 5;
```
