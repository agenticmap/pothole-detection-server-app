"""Audit the "potholes never co-locate" claim — read-only, writes nothing.

This file exists because that claim was published twice with two different
explanations, and **both were wrong**. Each mode below is one link in that chain,
kept runnable so the next person can re-derive it instead of trusting it. The
write-up is `docs/research/corroboration-coverage-analysis.md`.

## The claim

`crowd_sweep.py --reclassify` reported that of 243 pothole-classed observations,
**zero** have another from a different day within 25 m, against a day-matched null
of 10-35 with 0 of 30 draws reaching the observed value. Read as: the detector is
anti-reproducible.

## Wrong explanation #1 — instrument regimes (`--quarantine`)

Sessions split cleanly on `gbar_in_max / accel_max_g`: nine at 1.75-3.48 producing
0-4.7% potholes, three at 9.91-19.05 producing 20.4-24.2%, nothing between. Restrict
the null to one band and the same zero becomes ordinary (p ~ 0.49). That looked like
the pooled null was mixing two instruments.

**It is circular.** `gbar` is the classifier's dominant input, so selecting low-`gbar`
sessions *is* selecting low-pothole sessions; the resulting "not enough potholes for
power" was manufactured by the selection. Two checks kill it. `gbar/g` is not a session
property — every session is a mixture of the same two event types with near-identical
low tails (p10 = 0.84-1.12) differing only in the *fraction* of high-`gbar` events, and
that fraction is the pothole rate. And in grid cells both bands drove, the ordering
**reverses** (high band 2.60, low band 7.63). A real instrument state does not flip
depending on which road you look at.

`--quarantine` is kept, and kept labelled, because the reasoning error is the
instructive part.

## Wrong explanation #2 — none; the finding survived the clean control (`--device`)

Stratifying by device instead — which is not downstream of the classifier — the zero
holds: device 4eb6 alone, 223 potholes, observed 0 against a null of 2-27, 0/200 draws.
So the finding was real and needed a different explanation, not a different slice.

## The actual explanation (`--coverage`)

**91.9% of pothole-classed observations sit on road that only ONE day ever covered**
(mean 1.09 distinct days within 25 m), against 68.4% for everything else. They cannot
co-locate across days no matter how good the detector is. The null drew from all of
that day's observations — only 68.4% single-visit — so random draws had systematically
*more* opportunity than the real detections, and that gap is the entire effect.

Condition on road at least two days actually covered and only **18** pothole detections
remain in the whole dataset: observed 0 against a null of 0-7 whose own **median is 0**,
p ~ 0.64. There is nothing left to explain.

The corroboration failure is **route coverage**. Any future co-location claim must
condition its null on coverage or it will reproduce this artefact.

## Still standing

`--regimes` also measures sample rate, and that result is independent and real: device
a1878f6d puts 94.9% of its `time_in_max` off the 0.033548 s (29.81 Hz) grid that carries
**100%** of the other phone's 4,539 observations — it samples ~8x faster. Only 98
observations, and excluded from every clean test above, but two phones reporting
incomparable window features is worth knowing before a second device is added.

Usage (from the repo root):

    python scripts/session_regimes.py --regimes
    python scripts/session_regimes.py --coverage            # the decisive one
    python scripts/session_regimes.py --coverage --device 4eb6
    python scripts/session_regimes.py --quarantine          # the wrong turn, kept
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
# midpoint rather than tuned, so a session landing between the bands shows up as
# ambiguous instead of being silently absorbed into whichever side is closer.
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


class _Index:
    """Points bucketed into cells one search-radius wide.

    Both questions this script asks — "is there another point of a different day
    nearby" and "how many distinct days came near this point" — are the same
    neighbourhood walk, so they share one index. The brute-force version in
    crowd_sweep.py is fine for 243 points and takes minutes at 2,860, which is exactly
    the density the power control needs.
    """

    def __init__(self, rows: list[dict], radius_m: float) -> None:
        self.radius = radius_m
        lat0 = _median([r["lat"] for r in rows]) if rows else 0.0
        self.lon_scale = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
        self.dlat = radius_m / _M_PER_DEG_LAT
        self.dlon = radius_m / self.lon_scale if self.lon_scale else 1.0
        self.cells: dict[tuple[int, int], list[dict]] = {}
        for r in rows:
            self.cells.setdefault(self._cell(r), []).append(r)

    def _cell(self, r: dict) -> tuple[int, int]:
        return (int(r["lat"] / self.dlat), int(r["lon"] / self.dlon))

    def near(self, a: dict):
        """Every indexed point within `radius` of `a`, including `a` itself."""
        gy, gx = self._cell(a)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for b in self.cells.get((gy + dy, gx + dx), ()):
                    mx = (a["lon"] - b["lon"]) * self.lon_scale
                    my = (a["lat"] - b["lat"]) * _M_PER_DEG_LAT
                    if math.hypot(mx, my) <= self.radius:
                        yield b


def _colocated(picked: list[dict], key: str, radius_m: float) -> int:
    """How many of `picked` have another of `picked` with a DIFFERENT `key` nearby."""
    if not picked:
        return 0
    idx = _Index(picked, radius_m)
    hits = 0
    for a in picked:
        hits += any(
            b["client_id"] != a["client_id"] and b[key] != a[key] for b in idx.near(a)
        )
    return hits


def _annotate_coverage(rows: list[dict], radius_m: float) -> None:
    """Set r["cov"] = how many distinct DAYS drove within `radius` of r.

    Counted over ALL observations of any class, so this is a property of where the
    driver went, not of what the classifier decided. That independence is the whole
    point — it is what `--quarantine`'s band split lacked.
    """
    idx = _Index(rows, radius_m)
    for a in rows:
        a["cov"] = len({b["day"] for b in idx.near(a)})


def _null(
    pool_rows: list[dict], picked: list[dict], key: str, radius_m: float, draws: int
) -> list[int]:
    """Co-location for random subsets matched to `picked`'s per-`key` counts.

    The draw pool is whatever `pool_rows` is. Pass the population `picked` was actually
    eligible to be drawn from — matching per-day counts alone is NOT enough, which is
    the mistake this whole script documents. `--coverage` passes the coverage-eligible
    subset for exactly that reason.
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


async def _load(pool, args) -> tuple[list[dict], dict[str, str]]:
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_SESSION_SQL, args.pass_gap)]
    if args.device:
        rows = [r for r in rows if r["sess"].startswith(args.device)]

    band = {}
    for sess, rs in _sessions(rows).items():
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


def _classes(rows: list[dict]) -> list[str]:
    return sorted({r["sensor_class"] for r in rows if r["sensor_class"]})


async def run_regimes(pool, args) -> int:
    rows, band = await _load(pool, args)
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
        "\nThe off-grid column is the real result here: a session far off the 29.81 Hz\n"
        "grid sampled at a different rate, and two phones reporting incomparable window\n"
        "features is worth knowing before a second device is added.\n"
        "\nThe band column is NOT a real result — see --quarantine and the module\n"
        "docstring. gbar/g is the classifier's own input, so banding on it is circular."
    )
    return 0


async def run_coverage(pool, args) -> int:
    """The decisive mode: was the road ever driven twice where the potholes were?"""
    rows, _ = await _load(pool, args)
    _annotate_coverage(rows, args.radius)

    n_days = len({r["day"] for r in rows})
    print(
        f"{len(rows)} observations over {n_days} days"
        f"{' (device ' + args.device + ')' if args.device else ''}\n"
    )
    print(f"How many distinct DAYS drove within {args.radius:.0f} m of each observation?")
    print(f"\n  {'class':<18} {'n':>6} {'mean days':>10} {'single-day road':>17}")

    def line(label: str, sub: list[dict]) -> None:
        if not sub:
            return
        one = sum(1 for r in sub if r["cov"] == 1)
        mean = sum(r["cov"] for r in sub) / len(sub)
        print(f"  {label:<18} {len(sub):>6} {mean:>10.2f} {one / len(sub):>16.1%}")

    for cls in _classes(rows):
        line(cls, [r for r in rows if r["sensor_class"] == cls])
    line("-- everything else", [r for r in rows if r["sensor_class"] != "pothole"])
    line("-- all", rows)

    # The conditioned test. Restricting BOTH the picked set and the draw pool to road
    # that was actually revisited is the control the original null lacked: it compared
    # detections that were 92% ineligible against random draws that were only 68%.
    elig = [r for r in rows if r["cov"] >= 2]
    print(
        f"\nConditioned on road covered by >= 2 days: {len(elig)} observations "
        f"({len(elig) / len(rows):.1%} of the data)\n"
    )
    print(f"  {'class':<10} {'n':>6} {'observed':>9} {'null':>10} {'median':>7} {'draws<=obs':>11}")
    for cls in _classes(elig):
        picked = [r for r in elig if r["sensor_class"] == cls]
        if not picked:
            continue
        obs = _colocated(picked, "day", args.radius)
        nl = _null(elig, picked, "day", args.radius, args.draws)
        le = sum(1 for v in nl if v <= obs)
        print(
            f"  {cls:<10} {len(picked):>6} {obs:>9} "
            f"{str(nl[0]) + '-' + str(nl[-1]):>10} "
            f"{_median([float(v) for v in nl]):>7.0f} {f'{le}/{len(nl)}':>11}"
        )
    print(
        "\nIf the pothole row's null median is 0, the dataset never gave a pothole a\n"
        "second chance to be seen and the zero means nothing about the detector.\n"
        "See docs/research/corroboration-coverage-analysis.md."
    )
    return 0


async def run_quarantine(pool, args) -> int:
    """Kept as the documented WRONG TURN. Banding on gbar/g is circular — see docstring."""
    rows, band = await _load(pool, args)
    print(
        "NOTE: this mode is retained to reproduce a retracted result. Splitting on\n"
        "gbar/g splits on the classifier's own dominant input, so the low band is\n"
        "'sessions with few potholes' by construction. Use --coverage.\n"
    )
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
    return 0


async def run_power(pool, args) -> int:
    """Density control: at what n does co-location beat its null at all?"""
    rows, _ = await _load(pool, args)
    print(
        f"{len(rows)} observations, {len({r['day'] for r in rows})} days, "
        f"{len({r['sess'] for r in rows})} sessions\n"
    )
    print(f"{'class':<10} {'n':>6} {'observed':>9} {'null':>12} {'median':>7} {'p(>=obs)':>9}")
    for cls in _classes(rows):
        picked = [r for r in rows if r["sensor_class"] == cls]
        if len(picked) < 5:
            continue
        obs = _colocated(picked, "day", args.radius)
        nl = _null(rows, picked, "day", args.radius, args.draws)
        p = sum(1 for v in nl if v >= obs) / len(nl)
        print(
            f"{cls:<10} {len(picked):>6} {obs:>9} "
            f"{str(nl[0]) + '-' + str(nl[-1]):>12} "
            f"{_median([float(v) for v in nl]):>7.0f} {p:>9.3f}"
        )
    print(
        "\nThis null matches per-day counts only, so it does NOT control for coverage\n"
        "and will overstate what the real detections could have achieved. It answers\n"
        "one question honestly — whether the geometry can detect co-location at any\n"
        "density — and nothing more.\n"
        "\nNote: section 3's _energy_order swap exchanges the `crack` and `not` LABELS,\n"
        "so the dense class cannot be named with confidence."
    )
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--coverage",
        action="store_true",
        help="Was the road ever driven twice where the potholes were? (the decisive test)",
    )
    mode.add_argument(
        "--regimes", action="store_true", help="Per-session sample rate and gbar/g"
    )
    mode.add_argument(
        "--quarantine",
        action="store_true",
        help="Reproduce the retracted instrument-regime result (kept as a wrong turn)",
    )
    mode.add_argument(
        "--power", action="store_true", help="At what density does co-location beat its null?"
    )
    ap.add_argument(
        "--device",
        default=None,
        help="Restrict to one device by its short id, e.g. 4eb6. Device is the only "
        "stratification here that is not downstream of the classifier",
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
        if args.coverage:
            return await run_coverage(pool, args)
        if args.regimes:
            return await run_regimes(pool, args)
        if args.quarantine:
            return await run_quarantine(pool, args)
        return await run_power(pool, args)
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
