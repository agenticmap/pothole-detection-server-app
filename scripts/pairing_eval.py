"""Measure the pairing search — read-only, writes nothing.

Phase 2.2d. The pairing search decides which camera frame's verdict attaches to
which sensor event. Two jobs here:

1. **`--diff`** — re-rank the live candidate set under the pre-2.2d ranking and the
   lookahead cost, and report how often they disagree. This is where the headline
   number came from (713 of 2197 frames, 32.5%, on pothole_db). It lives in a script
   rather than a transcript so the figure is reproducible after the code changes,
   and so a drift in the cost weights shows up as a changed number.

2. **`--fit-lead`** — replace the guessed lead band with a measured one. The
   camera's usable ground-distance band is a property of the lens and the mount
   pitch, and `FUSION_LEAD_NEAR_M` / `FUSION_LEAD_FAR_M` currently ship as a
   HYPOTHESIS: only 3 potholes in pothole_db have a paired frame at all, which is
   far too few to fit anything. Once Phase 2.7's ~300 frame labels exist, this mode
   reports the observed delta_m distribution for frames a human confirmed contain a
   pothole, and prints the p5/p95 band to put in the config.

Both modes run entirely in SQL against the live tables and take no locks beyond a
read snapshot, so pointing this at the dev database is safe. Nothing here writes,
and unlike the fusion job it does not mark frames processed.

Usage (from the repo root):

    python scripts/pairing_eval.py --diff
    python scripts/pairing_eval.py --diff --window-m 60      # try a wider search
    python scripts/pairing_eval.py --fit-lead

Cost parameters default to the values in app/config.py, so a --diff run reflects
what the job would actually do; override them to explore.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Run directly (`python scripts/pairing_eval.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402

# Every (frame, observation) pair the search could consider, ranked twice: once by
# the pre-2.2d keys and once by the lookahead cost. Deliberately NOT importing
# _PAIRING_SQL from the service: that query is scoped to one unprocessed batch,
# whereas this needs the whole table, and the two rankings have to sit side by side
# in one pass or the comparison is not apples to apples.
#
# The spatial and temporal gates use the WIDER of the two windows so neither
# ranking is handed a candidate set the other never saw.
# $1 = window_m, $2 = window_ms_max, $3 = w_lead, $4 = lead_near, $5 = lead_far,
# $6 = w_kinematic, $7 = speed_floor, $8 = forward_penalty.
_DIFF_SQL = """
WITH cand AS (
    SELECT
        f.client_id      AS frame_id,
        o.client_id      AS event_id,
        o.sensor_class,
        EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))          AS delta_s,
        ST_Distance(f.geom, o.geom)                        AS delta_m,
        GREATEST(COALESCE(o.speed_mps, 0.0), $7)           AS speed
    FROM asset_frame f
    JOIN asset_observation o
      ON o.device_id = f.device_id
     AND ST_DWithin(f.geom, o.geom, $1)
     AND abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) * 1000 < $2
),
scored AS (
    SELECT
        cand.*,
        (
            $3 * (GREATEST(0.0, $4 - delta_m) + GREATEST(0.0, delta_m - $5))
          + $6 * abs(delta_s + delta_m / speed)
          + CASE WHEN delta_s > 0 THEN $8 ELSE 0.0 END
        ) AS cost
    FROM cand
),
ranked AS (
    SELECT
        scored.*,
        ROW_NUMBER() OVER (
            PARTITION BY frame_id
            ORDER BY abs(delta_s) ASC, delta_m ASC, event_id ASC
        ) AS rn_legacy,
        ROW_NUMBER() OVER (
            PARTITION BY frame_id ORDER BY cost ASC, event_id ASC
        ) AS rn_cost
    FROM scored
)
SELECT
    -- Bucket by the speed of the observation the OLD ranking chose, so the report
    -- says where the disagreement lives rather than only how big it is.
    CASE
        WHEN speed < 8.333 THEN 'slow (<30 km/h)'
        WHEN speed < 16.667 THEN 'mid (30-60 km/h)'
        ELSE 'fast (>60 km/h)'
    END                                              AS speed_bucket,
    COALESCE(sensor_class, 'unscored')               AS sensor_class,
    count(*)                                         AS frames,
    count(*) FILTER (WHERE rn_cost = 1)              AS agree,
    avg(delta_m)                                     AS mean_delta_m,
    avg(delta_s)                                     AS mean_delta_s,
    avg(cost)                                        AS mean_cost_of_legacy_pick
FROM ranked
WHERE rn_legacy = 1
GROUP BY 1, 2
ORDER BY 1, 2
"""

# Candidate geometry for frames a human confirmed contain a pothole. label = 1 is
# "pothole present" (migration 010). No ranking here: the point is the distribution
# of delta_m over TRUE positives, which is what the lead band should span.
# $1 = window_m, $2 = window_ms_max, $3 = speed_floor.
_FIT_LEAD_SQL = """
WITH confirmed AS (
    SELECT f.client_id, f.device_id, f.ts_utc, f.geom
    FROM asset_frame f
    JOIN frame_label l ON l.frame_client_id = f.client_id
    WHERE l.label = 1
),
cand AS (
    SELECT
        c.client_id AS frame_id,
        ST_Distance(c.geom, o.geom)                AS delta_m,
        EXTRACT(EPOCH FROM (c.ts_utc - o.ts_utc))  AS delta_s,
        GREATEST(COALESCE(o.speed_mps, 0.0), $3)   AS speed,
        ROW_NUMBER() OVER (
            PARTITION BY c.client_id
            -- Nearest in time, i.e. the PRE-2.2d choice, on purpose: fitting the
            -- band with the cost that the band parameterises would be circular.
            ORDER BY abs(EXTRACT(EPOCH FROM (c.ts_utc - o.ts_utc))) ASC, o.client_id ASC
        ) AS rn
    FROM confirmed c
    JOIN asset_observation o
      ON o.device_id = c.device_id
     AND ST_DWithin(c.geom, o.geom, $1)
     AND abs(EXTRACT(EPOCH FROM (c.ts_utc - o.ts_utc))) * 1000 < $2
     AND o.sensor_class = 'pothole'
)
SELECT
    count(*)                                                        AS n,
    percentile_cont(0.05) WITHIN GROUP (ORDER BY delta_m)           AS p05_m,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY delta_m)           AS p50_m,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY delta_m)           AS p95_m,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY delta_s)           AS p50_s,
    avg(speed)                                                      AS mean_speed
FROM cand
WHERE rn = 1
"""

_COUNT_LABELS_SQL = """
SELECT count(*) FROM frame_label WHERE label = 1
"""

# Whether migration 010 has been applied at all. detect_eval.py uses the same probe.
_HAS_FRAME_LABEL_SQL = "SELECT to_regclass('public.frame_label') IS NOT NULL"

# The minimum number of confirmed frames worth fitting a p5/p95 band from. Below
# this the percentiles are noise dressed as a measurement, and printing them would
# invite someone to paste them into the config.
_MIN_LABELS_TO_FIT = 30


async def run_diff(pool, args) -> int:
    rows = await pool.fetch(
        _DIFF_SQL,
        args.window_m,
        float(args.window_ms_max),
        args.w_lead,
        args.lead_near_m,
        args.lead_far_m,
        args.w_kinematic,
        args.speed_floor,
        args.forward_penalty,
    )
    if not rows:
        print("No candidate pairs at all. Nothing to compare.")
        print(
            f"  Widen the search (--window-m is {args.window_m:g} m, "
            f"--window-ms-max {args.window_ms_max} ms) or check that frames and "
            "observations share a device_id and overlap in time."
        )
        return 0

    total = sum(r["frames"] for r in rows)
    agree = sum(r["agree"] for r in rows)
    changed = total - agree

    print("Pairing search: pre-2.2d ranking vs the lookahead cost")
    print(f"  lead band      {args.lead_near_m:g}-{args.lead_far_m:g} m")
    print(f"  weights        lead={args.w_lead:g} kinematic={args.w_kinematic:g} "
          f"forward={args.forward_penalty:g}")
    print(f"  search window  {args.window_m:g} m, <= {args.window_ms_max} ms "
          f"(speed floor {args.speed_floor:g} m/s)")
    print()
    header = (
        f"{'speed bucket':<18} {'class':<10} {'frames':>7} {'changed':>8} "
        f"{'%':>6} {'mean dm':>9} {'mean dt':>9} {'cost@old':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        n, ag = r["frames"], r["agree"]
        ch = n - ag
        print(
            f"{r['speed_bucket']:<18} {r['sensor_class']:<10} {n:>7} {ch:>8} "
            f"{100.0 * ch / n:>5.1f}% {r['mean_delta_m']:>8.1f}m "
            f"{r['mean_delta_s']:>8.2f}s {r['mean_cost_of_legacy_pick']:>9.2f}"
        )
    print("-" * len(header))
    pct = 100.0 * changed / total
    print(f"{'TOTAL':<18} {'':<10} {total:>7} {changed:>8} {pct:>5.1f}%")
    print()
    print(
        f"{changed} of {total} frames ({pct:.1f}%) would pair with a different "
        "observation."
    )
    print(
        "  A high 'cost@old' column means the old ranking was choosing frames far "
        "outside the lead band -- i.e. frames taken where the camera could not have "
        "seen the pothole."
    )
    return 0


async def run_fit_lead(pool, args) -> int:
    if not await pool.fetchval(_HAS_FRAME_LABEL_SQL):
        print("Table frame_label does not exist here; apply migration 010 first.")
        return 1

    n_labels = await pool.fetchval(_COUNT_LABELS_SQL)
    if n_labels < _MIN_LABELS_TO_FIT:
        print(
            f"Only {n_labels} frame(s) are labelled as containing a pothole; "
            f"{_MIN_LABELS_TO_FIT} is the floor for fitting a band."
        )
        print(
            "  Refusing to print percentiles from this few rows: they would be noise "
            "wearing a measurement's clothes, and someone would paste them into "
            "FUSION_LEAD_NEAR_M."
        )
        print("  Label frames with: python scripts/label_frames.py")
        return 1

    row = await pool.fetchrow(
        _FIT_LEAD_SQL, args.window_m, float(args.window_ms_max), args.speed_floor
    )
    if not row or not row["n"]:
        print(
            f"{n_labels} confirmed frame(s), but none has a pothole-classed "
            "observation inside the search window. Nothing to fit."
        )
        return 1

    print(f"Lead band fitted from {row['n']} confirmed pothole frame(s)")
    print(f"  mean speed        {row['mean_speed']:.2f} m/s")
    print(f"  delta_m  p05      {row['p05_m']:.1f} m")
    print(f"  delta_m  p50      {row['p50_m']:.1f} m")
    print(f"  delta_m  p95      {row['p95_m']:.1f} m")
    print(f"  delta_s  p50      {row['p50_s']:+.2f} s")
    print()
    print("Put these in .env (replacing the shipped hypothesis):")
    print(f"  FUSION_LEAD_NEAR_M={row['p05_m']:.1f}")
    print(f"  FUSION_LEAD_FAR_M={row['p95_m']:.1f}")
    print(
        f"  FUSION_WINDOW_M={max(args.window_m, row['p95_m'] * 1.5):.0f}  "
        "# must comfortably exceed the far edge"
    )
    if row["p50_s"] > 0:
        print()
        print(
            "NOTE: the median delta_s is POSITIVE, i.e. confirmed frames are being "
            "matched to events that fired BEFORE the photo. That contradicts the "
            "lookahead model and is worth understanding before trusting the band -- "
            "check for a clock offset between the camera and sensor writers."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Measure the pairing search (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--diff", action="store_true",
        help="compare the pre-2.2d ranking against the lookahead cost",
    )
    mode.add_argument(
        "--fit-lead", action="store_true",
        help="fit the lead band from human-confirmed frames (needs frame_label)",
    )
    p.add_argument("--window-m", type=float, default=settings.fusion_window_m)
    p.add_argument("--window-ms-max", type=int, default=settings.fusion_window_ms_max)
    p.add_argument("--lead-near-m", type=float, default=settings.fusion_lead_near_m)
    p.add_argument("--lead-far-m", type=float, default=settings.fusion_lead_far_m)
    p.add_argument("--w-lead", type=float, default=settings.fusion_w_lead)
    p.add_argument("--w-kinematic", type=float, default=settings.fusion_w_kinematic)
    p.add_argument("--speed-floor", type=float, default=settings.fusion_speed_floor_mps)
    p.add_argument(
        "--forward-penalty", type=float, default=settings.fusion_forward_penalty
    )
    return p


async def main_async(args) -> int:
    pool = await create_pool()
    try:
        if args.diff:
            return await run_diff(pool, args)
        return await run_fit_lead(pool, args)
    finally:
        await pool.close()


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
