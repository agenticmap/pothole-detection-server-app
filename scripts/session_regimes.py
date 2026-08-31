"""Detect per-session instrument regimes, and test co-location within one — read-only.

This exists because `crowd_sweep.py --reclassify` produced a result that was
statistically significant and wrong, and nothing in the codebase could have caught
it. It reported that pothole detections co-locate across days **zero** times against
a day-matched null of 10-35, 0 of 30 draws reaching the observed value, and concluded
the detector was anti-reproducible.

The null controlled for total count, for per-day counts and therefore for sparsity.
It did not control for the population being **heterogeneous**, and it is not.

## The two regimes

Sessions (a 20-minute gap in a device's timeline, the same rule the clustering job
uses for its pass key) split cleanly on `gbar_in_max / accel_max_g` — window energy
over peak acceleration. Nine sessions sit at 1.75-3.48 and produce 0-4.7% potholes;
three sit at 9.91-19.05 and produce 20.4-24.2%. Nothing falls between 3.48 and 9.91.

The raw statistics do NOT separate them: `accel_max_g` medians run 1.64-3.40 and
`accel_std` 0.45-0.74, and both bands span that whole range. The roads and the forces
were comparable. Only the app's DERIVED window features differ — `magnitude` 4.1x,
`gbar_in_max` 5.0x — and those are what the classifier reads.

At least two causes, and the modes below distinguish them:

- **Sample rate.** `time_in_max` is quantised. One phone puts 100% of its
  observations on a 0.033548 s grid (29.81 Hz, a 15-sample window); the other is
  94.9% off it, on a grid 8x finer (~238 Hz). A window feature summed over 8x the
  samples is inflated for free.
- **Something per-session.** One day's sessions stayed on the 29.81 Hz grid, same
  device, yet ran 3.6x their own usual ratio. Not a constant gain — the percentile
  curves cross at p10 and diverge above, so it is a mixture with more sustained-energy
  events. Mount or vehicle fits; this data cannot separate those.

## Why the split matters more than the diagnosis

Pooled, the zero looks impossible. Inside one regime the SAME zero has p ~ 0.49
against a null whose own median is 2. The high band contributes over half the
detections from two days, and permutation scatters those freely across the pooled
data while the real detections cannot cross regimes at all. That inflated the null
and manufactured the significance.

`--power` is the control that keeps the retraction honest: if nothing co-located at
any density the method would be vacuous. The dense class co-locates ABOVE the top of
its own null, so the geometry and the null work, and the pothole class at n=106 over
5 days is simply below the density this test can resolve.

Usage (from the repo root):

    python scripts/session_regimes.py --regimes
    python scripts/session_regimes.py --quarantine
    python scripts/session_regimes.py --power
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402

# Anywhere in the empty 3.48-9.91 corridor gives the same partition. Picked at the
# midpoint rather than tuned, so a new session that lands between the bands shows up
# as ambiguous instead of being silently absorbed into whichever side is closer.
BAND_SPLIT = 6.0

# The accelerometer grid the main phone runs on: 15 samples over ~0.5 s. Derived from
# the data (the modal step between distinct time_in_max values), not from the app.
GRID_S = 0.033548
GRID_TOL = 0.001

_M_PER_DEG_LAT = 111_320.0

_SESSION_SQL = """
WITH t AS (
  SELECT client_id, device_id, ts_utc, sensor_class, gbar_in_max, accel_max_g,
         magnitude, accel_std, time_in_max,
         ST_X(geom::geometry) AS lon, ST_Y(geom::geometry) AS lat,
         CASE WHEN lag(ts_utc) OVER w IS NULL
               OR ts_utc - lag(ts_utc) OVER w > make_interval(mins => $1)
              THEN 1 ELSE 0 END AS is_new
  FROM asset_observation
  WINDOW w AS (PARTITION BY device_id ORDER BY ts_utc, client_id))
SELECT client_id, sensor_class, lon, lat, gbar_in_max, accel_max_g, magnitude,
       accel_std, time_in_max,
       (ts_utc AT TIME ZONE 'America/Toronto')::date::text AS day,
       right(device_id, 4) || ':' ||
         sum(is_new) OVER (PARTITION BY device_id ORDER BY ts_utc, client_id
                           ROWS UNBOUNDED PRECEDING)::text AS sess
FROM t ORDER BY ts_utc, client_id
"""


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _on_grid(t: float | None) -> bool:
    """Is this peak position a multiple of the 29.81 Hz sample period?"""
    if t is None:
        return False
    r = math.fmod(t, GRID_S)
    return r < GRID_TOL or abs(r - GRID_S) < GRID_TOL


def _colocated(picked: list[dict], key: str, radius_m: float) -> int:
    """How many of `picked` have another of `picked` with a DIFFERENT `key` nearby.

    Grid-bucketed at the search radius, so this is O(n) rather than O(n^2). The
    brute-force version in crowd_sweep.py is fine for 243 points and takes minutes at
    2,860, which is exactly the density the power control needs.
    """
    if not picked:
        return 0
    lat0 = _median([r["lat"] for r in picked])
    lon_scale = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    dlat = radius_m / _M_PER_DEG_LAT
    dlon = radius_m / lon_scale

    grid: dict[tuple[int, int], list[dict]] = {}
    for r in picked:
        grid.setdefault((int(r["lat"] / dlat), int(r["lon"] / dlon)), []).append(r)

    hits = 0
    for a in picked:
        gy, gx = int(a["lat"] / dlat), int(a["lon"] / dlon)
        found = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for b in grid.get((gy + dy, gx + dx), ()):
                    if a["client_id"] == b["client_id"] or a[key] == b[key]:
                        continue
                    mx = (a["lon"] - b["lon"]) * lon_scale
                    my = (a["lat"] - b["lat"]) * _M_PER_DEG_LAT
                    if math.hypot(mx, my) <= radius_m:
                        found = True
                        break
                if found:
                    break
            if found:
                break
        hits += found
    return hits


def _null(
    pool_rows: list[dict], picked: list[dict], key: str, radius_m: float, draws: int
) -> list[int]:
    """Co-location for random subsets matched to `picked`'s per-`key` counts.

    Crucially the draw pool is whatever `pool_rows` is — pass a single regime and the
    null is built from that regime alone. Passing the whole database while `picked`
    comes from one regime is the exact mistake this script documents.
    """
    pools: dict = {}
    for r in pool_rows:
        pools.setdefault(r[key], []).append(r)
    counts: dict = {}
    for r in picked:
        counts[r[key]] = counts.get(r[key], 0) + 1

    rng = random.Random(11)  # deterministic, so a rerun reproduces the interval
    out = []
    for _ in range(draws):
        sample: list[dict] = []
        for k, n in counts.items():
            p = pools.get(k, [])
            sample += rng.sample(p, min(n, len(p)))
        out.append(_colocated(sample, key, radius_m))
    return sorted(out)


async def _load(pool, gap_minutes: int) -> tuple[list[dict], dict[str, str]]:
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_SESSION_SQL, gap_minutes)]

    by_sess: dict[str, list[dict]] = {}
    for r in rows:
        by_sess.setdefault(r["sess"], []).append(r)

    band = {}
    for sess, rs in by_sess.items():
        ratios = [
            r["gbar_in_max"] / r["accel_max_g"]
            for r in rs
            if r["gbar_in_max"] is not None and r["accel_max_g"]
        ]
        band[sess] = "high" if _median(ratios) >= BAND_SPLIT else "low"
    return rows, band


def _sessions(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["sess"], []).append(r)
    return out


async def run_regimes(pool, args) -> int:
    rows, band = await _load(pool, args.pass_gap)
    print(
        f"{'session':<10} {'n':>5} {'gbar/g':>8} {'accel_g':>8} {'std':>6} "
        f"{'off-grid':>9} {'band':>5} {'pothole':>8}"
    )
    for sess, rs in sorted(_sessions(rows).items()):
        if len(rs) < args.min_session:
            continue
        ratios = [
            r["gbar_in_max"] / r["accel_max_g"]
            for r in rs
            if r["gbar_in_max"] is not None and r["accel_max_g"]
        ]
        off = 100.0 * sum(1 for r in rs if not _on_grid(r["time_in_max"])) / len(rs)
        pot = 100.0 * sum(1 for r in rs if r["sensor_class"] == "pothole") / len(rs)
        print(
            f"{sess:<10} {len(rs):>5} {_median(ratios):>8.2f} "
            f"{_median([r['accel_max_g'] for r in rs if r['accel_max_g'] is not None]):>8.2f} "
            f"{_median([r['accel_std'] for r in rs if r['accel_std'] is not None]):>6.3f} "
            f"{off:>8.1f}% {band[sess]:>5} {pot:>7.1f}%"
        )
    print(
        "\nA session far off the 29.81 Hz grid sampled at a different rate; a high\n"
        "gbar/g with an ordinary accel_g did not. Both break comparability, and a\n"
        "cluster must not mix them."
    )
    return 0


async def run_quarantine(pool, args) -> int:
    rows, band = await _load(pool, args.pass_gap)
    for label, keep in (
        ("both regimes pooled (this is what --reclassify did)", lambda s: True),
        ("low band only", lambda s: band[s] == "low"),
        ("high band only", lambda s: band[s] == "high"),
    ):
        sub = [r for r in rows if keep(r["sess"])]
        pot = [r for r in sub if r["sensor_class"] == "pothole"]
        print(f"\n=== {label} ===")
        print(f"  {len(sub)} observations, {len(pot)} pothole-classed")
        for key, unit in (("day", "day"), ("sess", "session")):
            n_units = len({r[key] for r in sub})
            if n_units < 2:
                print(f"  cross-{unit}: only {n_units} present, skipped")
                continue
            obs = _colocated(pot, key, args.radius)
            nl = _null(sub, pot, key, args.radius, args.draws)
            le = sum(1 for v in nl if v <= obs)
            print(
                f"  cross-{unit:<8} ({n_units:>2}): observed {obs:>4}   "
                f"null {nl[0]}-{nl[-1]} (median {_median([float(v) for v in nl]):.0f})   "
                f"draws <= observed: {le}/{len(nl)}"
            )
    print(
        "\nPooled, zero looks impossible. Within one regime it is ordinary. The pooled\n"
        "null is the artefact, not the zero."
    )
    return 0


async def run_power(pool, args) -> int:
    rows, band = await _load(pool, args.pass_gap)
    low = [r for r in rows if band[r["sess"]] == "low"]
    print(
        f"LOW band: {len(low)} observations, {len({r['day'] for r in low})} days, "
        f"{len({r['sess'] for r in low})} sessions\n"
    )
    print(f"{'class':<10} {'n':>6} {'observed':>9} {'null':>12} {'median':>7} {'p(>=obs)':>9}")
    classes = sorted({r["sensor_class"] for r in low if r["sensor_class"]})
    for cls in classes:
        picked = [r for r in low if r["sensor_class"] == cls]
        if len(picked) < 5:
            continue
        obs = _colocated(picked, "day", args.radius)
        nl = _null(low, picked, "day", args.radius, args.draws)
        p = sum(1 for v in nl if v >= obs) / len(nl)
        print(
            f"{cls:<10} {len(picked):>6} {obs:>9} "
            f"{str(nl[0]) + '-' + str(nl[-1]):>12} "
            f"{_median([float(v) for v in nl]):>7.0f} {p:>9.3f}"
        )
    print(
        "\nIf the dense class beats its null, the geometry and the null work and the\n"
        "pothole class is merely too sparse to resolve. If nothing beats it at any\n"
        "density, this whole test is vacuous and the retraction it supports is too.\n"
        "\nNote: section 3's _energy_order swap exchanges the `crack` and `not` LABELS,\n"
        "so the dense class cannot be named with confidence. The power result does not\n"
        "depend on which name is right."
    )
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--regimes", action="store_true", help="Per-session instrument fingerprint"
    )
    mode.add_argument(
        "--quarantine",
        action="store_true",
        help="Cross-day co-location pooled vs within each regime",
    )
    mode.add_argument(
        "--power",
        action="store_true",
        help="Positive control: at what density does co-location beat its null?",
    )
    ap.add_argument("--pass-gap", type=int, default=settings.cluster_pass_gap_minutes)
    ap.add_argument("--radius", type=float, default=25.0, help="Co-location radius (m)")
    ap.add_argument("--draws", type=int, default=200, help="Permutation draws")
    ap.add_argument(
        "--min-session", type=int, default=15, help="Ignore sessions below this size"
    )
    args = ap.parse_args()

    pool = await create_pool()
    try:
        if args.regimes:
            return await run_regimes(pool, args)
        if args.quarantine:
            return await run_quarantine(pool, args)
        return await run_power(pool, args)
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
