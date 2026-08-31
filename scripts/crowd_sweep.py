"""Measure the crowd pipeline's parameters — read-only, writes nothing.

Three questions this answers, none of which anyone has answered from data:

1. **`--sweep`** — what do `CLUSTER_EPS_M`, `CLUSTER_MIN_POINTS`,
   `CLUSTER_MIN_DISTINCT_PASSES` and `CLUSTER_MIN_DISTINCT_DEVICES` actually cost
   and buy on this database? Every one of them shipped as an a-priori guess, and
   this project has now been wrong three times about which knob mattered (the
   severity scale, the outlier feature set, and `min_points`).

2. **`--accumulate`** — the shape of Sattar et al.'s Figure 9: how does the result
   change as surveys accumulate? They report 65% accuracy after one survey, >90%
   after three, 100% after five, measured against field inspection.

   **This script cannot measure accuracy.** There is no ground truth in the
   database, so it measures SELF-CONSISTENCY instead: saturation, persistence,
   centroid drift and class stability. A consistently wrong detection scores
   perfectly here. Treat a flat curve as "more surveys stop changing the answer",
   never as "the answer is right".

## How it stays read-only

`_compute_clusters` in app/fusion/service.py is a pure-SELECT seam, deliberately
separated from the write phase. This script calls it, plus the pure-Python
`_direction_split_clusters` and `_integrate_cluster_row`, and never reaches the
transaction that writes `asset_cluster` / `observation_cluster_link` /
`cluster_run`. It also never takes the clustering advisory lock, so it cannot
block or be blocked by the scheduled job.

Parameters follow the `pairing_eval.py` convention: every flag defaults to the
live setting, and the settings singleton is never mutated — so a run with no
flags reports exactly what the job would do.

3. **`--reclassify`** — is the *classifier* the reason nothing ever corroborates?

   The paper fits DPGMM "on the collected data for each road segment ... for each
   time of road surface anomaly data collection" (its §5.2), so the class
   assignment is RELATIVE to one survey. The server fits one GMM over everything,
   making `pothole` an absolute cut at the extreme tail of the whole dataset. If
   that is why the same defect hit at 40 km/h on Monday and 60 km/h on Thursday
   lands in different classes, re-classifying per survey should restore cross-day
   co-location.

   This mode re-runs classification three ways over the same rows and reports how
   many pothole-classed observations have another pothole-classed observation from
   a DIFFERENT DAY nearby. It never touches `sensor_model`.

Usage (from the repo root):

    python scripts/crowd_sweep.py --sweep
    python scripts/crowd_sweep.py --sweep --eps 10 15 25 40 --min-points 1 2 3
    python scripts/crowd_sweep.py --accumulate
    python scripts/crowd_sweep.py --reclassify
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Run directly (`python scripts/crowd_sweep.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402
from app.fusion.service import (  # noqa: E402
    _compute_clusters,
    _direction_split_clusters,
)
from app.sensor_model import features as feat  # noqa: E402

# Below this many surveys the accumulation curve is two points and a straight
# line through them. pairing_eval.py refuses to fit a lead band under 30 labels
# for the same reason: a number nobody should act on is worse than no number.
_MIN_SURVEYS_TO_CURVE = 3

# Two clusters within this distance across consecutive accumulation steps are
# treated as the same defect. Generous on purpose -- the question is "did this
# defect survive another survey", not "did its centroid stay put", which is
# reported separately as drift.
_SAME_DEFECT_M = 30.0

_M_PER_DEG_LAT = 111_320.0


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Flat-earth distance between (lon, lat) pairs. Same approximation as
    _member_distances in the clustering job, and for the same reason."""
    lat_scale = math.cos(math.radians((a[1] + b[1]) / 2.0))
    dx = (a[0] - b[0]) * _M_PER_DEG_LAT * lat_scale
    dy = (a[1] - b[1]) * _M_PER_DEG_LAT
    return math.hypot(dx, dy)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


async def _cluster_once(conn, *, eps_m, min_points, window, min_conf, gap, as_of=None):
    """One what-if clustering run. Returns the cluster rows the job would write."""
    clusters, n_members, _, _ = await _compute_clusters(
        conn,
        window=window,
        min_conf=min_conf,
        eps_m=eps_m,
        min_points=min_points,
        pass_gap_minutes=gap,
        as_of=as_of,
    )
    if clusters is None:
        # Distinct from "zero clusters found": the global member count did not
        # reach min_points, so DBSCAN never ran.
        return None, n_members
    if settings.cluster_bearing_aware:
        clusters = _direction_split_clusters(clusters, settings.cluster_bearing_tolerance_deg)
    return clusters, n_members


# ── Mode 1: parameter sweep ─────────────────────────────────────────────────


async def run_sweep(pool, args) -> None:
    print("Read-only parameter sweep. Nothing is written.\n")
    print(
        f"Fixed: window_days={args.window_days} member_min_confidence={args.min_conf} "
        f"pass_gap_minutes={args.pass_gap} bearing_aware={settings.cluster_bearing_aware}"
    )
    print()

    header = (
        f"{'eps_m':>6} {'min_pts':>8} │ {'clusters':>8} {'members':>8} {'noise':>6} "
        f"│ {'>=1 pass':>8} {'>=2':>5} {'>=3':>5} {'>=2 dev':>7} │ {'med span':>9}"
    )
    print(header)
    print("─" * len(header))

    async with pool.acquire() as conn:
        for eps_m in args.eps:
            for min_points in args.min_points:
                clusters, n_members = await _cluster_once(
                    conn, eps_m=eps_m, min_points=min_points,
                    window=args.window_days, min_conf=args.min_conf, gap=args.pass_gap,
                )
                if clusters is None:
                    print(
                        f"{eps_m:>6.0f} {min_points:>8} │ "
                        f"{'gate not met':>8} {n_members:>8}"
                    )
                    continue

                clustered = sum(int(c["observation_count"]) for c in clusters)
                passes = [int(c["distinct_passes"]) for c in clusters]
                devices = [int(c["distinct_devices"]) for c in clusters]
                spans = [float(c["member_span_s"] or 0.0) for c in clusters]
                marker = ""
                if eps_m == settings.cluster_eps_m and min_points == settings.cluster_min_points:
                    marker = "  ← configured"
                print(
                    f"{eps_m:>6.0f} {min_points:>8} │ {len(clusters):>8} {clustered:>8} "
                    f"{n_members - clustered:>6} │ "
                    f"{sum(1 for p in passes if p >= 1):>8} "
                    f"{sum(1 for p in passes if p >= 2):>5} "
                    f"{sum(1 for p in passes if p >= 3):>5} "
                    f"{sum(1 for d in devices if d >= 2):>7} │ "
                    f"{_median(spans):>9.1f}{marker}"
                )

    print()
    print("noise    = admitted members DBSCAN discarded (they reach no cluster, and")
    print("           without the raw-observations layer they are visible nowhere).")
    print("med span = median seconds between a cluster's first and last member. A few")
    print("           seconds means one drive-past, not corroboration.")


# ── Mode 2: survey accumulation ─────────────────────────────────────────────


async def _survey_boundaries(conn, window_days: int) -> list[datetime]:
    """End-of-day cutoffs, one per day on which anything was recorded.

    A "survey" here is a calendar day of collection, which is how the paper's own
    experiment was organised (five days, 21-30 March 2018). Per-device drives are
    already counted separately as passes.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT date_trunc('day', ts_utc) AS d
        FROM asset_observation
        WHERE received_at > now() - make_interval(days => $1)
        ORDER BY d
        """,
        window_days,
    )
    return [r["d"] + timedelta(days=1) for r in rows]


async def run_accumulate(pool, args) -> int:
    async with pool.acquire() as conn:
        cutoffs = await _survey_boundaries(conn, args.window_days)

        if len(cutoffs) < _MIN_SURVEYS_TO_CURVE:
            print(
                f"REFUSING TO DRAW A CURVE: only {len(cutoffs)} collection day(s) in the "
                f"last {args.window_days} days (need {_MIN_SURVEYS_TO_CURVE})."
            )
            print("Collect more surveys, or widen --window-days.")
            return 2

        print("Read-only survey accumulation. Nothing is written.\n")
        print(
            "This measures SELF-CONSISTENCY, not accuracy. There is no ground truth in\n"
            "the database, so a consistently wrong detection scores perfectly here. The\n"
            "paper's equivalent figure was validated by walking the road and counting.\n"
        )
        print(
            f"Parameters: eps_m={args.eps[0]} min_points={args.min_points[0]} "
            f"window_days={args.window_days} pass_gap_minutes={args.pass_gap}"
        )
        print()

        header = (
            f"{'surveys':>7} {'through':>12} │ {'members':>8} {'clusters':>8} "
            f"│ {'new':>5} {'kept':>5} {'lost':>5} {'persist':>8} │ {'drift m':>8} "
            f"│ {'max pass':>8}"
        )
        print(header)
        print("─" * len(header))

        previous: list[tuple[float, float]] = []
        for k, cutoff in enumerate(cutoffs, start=1):
            clusters, n_members = await _cluster_once(
                conn, eps_m=args.eps[0], min_points=args.min_points[0],
                window=args.window_days, min_conf=args.min_conf, gap=args.pass_gap,
                as_of=cutoff,
            )
            if clusters is None:
                print(f"{k:>7} {cutoff.date()!s:>12} │ {n_members:>8} {'gate not met':>8}")
                continue

            current = [(float(c["centroid_lon"]), float(c["centroid_lat"])) for c in clusters]
            max_pass = max((int(c["distinct_passes"]) for c in clusters), default=0)

            kept, drifts = 0, []
            for prev in previous:
                nearest = min((_metres(prev, cur) for cur in current), default=None)
                if nearest is not None and nearest <= _SAME_DEFECT_M:
                    kept += 1
                    drifts.append(nearest)
            lost = len(previous) - kept
            new = len(current) - kept
            persist = f"{100.0 * kept / len(previous):.0f}%" if previous else "—"

            print(
                f"{k:>7} {cutoff.date()!s:>12} │ {n_members:>8} {len(current):>8} │ "
                f"{new:>5} {kept:>5} {lost:>5} {persist:>8} │ "
                f"{_median(drifts):>8.1f} │ {max_pass:>8}"
            )
            previous = current

    print()
    print("persist  = share of the previous step's clusters still present at this one.")
    print("drift m  = median centroid movement for those. The paper claims repeat")
    print("           passes improve location accuracy; this is the honest test of it.")
    print("max pass = the highest distinct_passes any single cluster reached. If this")
    print("           stays at 1, no defect was ever seen twice and the crowdsourcing")
    print("           model has nothing to integrate -- which is the state as of")
    print("           2026-08-30. See docs/research/paper-fidelity-assessment.md.")
    return 0


# ── Mode 3: is the classifier why nothing corroborates? ────────────────────

# Below this many rows a 3-component GMM is fitting noise, so the survey is
# skipped rather than given a meaningless partition.
_MIN_ROWS_PER_SURVEY = 60


def _fit_and_label(rows, *, corrected_energy: bool) -> list[bool]:
    """Fit one 3-component GMM over `rows`; return "is pothole" per row.

    Mirrors app/sensor_model/fit.py — [ratio, gbar], z-scored, GaussianMixture
    k=3, top-energy component labelled pothole — with one switch.

    `corrected_energy=False` reproduces the shipped `_energy_order`, which ranks
    by norm in Z-SCORED space. That measures distance from the population MEAN,
    not energy, so a component well below the mean outranks one near it; on this
    data it swaps `crack` and `not`. It should not change WHICH component is
    pothole (the extreme tail is furthest under either rule) — included so that
    is demonstrated rather than assumed.
    """
    import numpy as np
    from sklearn.mixture import GaussianMixture

    x = np.array(
        [feat.classifier_features(r["magnitude"], r["accel_std"], r["gbar_in_max"]) for r in rows],
        dtype=np.float64,
    )
    mean, std = x.mean(axis=0), x.std(axis=0)
    std = np.where(std > 0, std, 1.0)
    z = (x - mean) / std
    if np.allclose(z.std(axis=0), 0.0):
        return [False] * len(rows)

    gmm = GaussianMixture(n_components=3, random_state=42).fit(z)
    # Un-standardise before ranking, so "energy" means energy.
    ranking = np.linalg.norm(gmm.means_ * std + mean, axis=1) if corrected_energy \
        else np.linalg.norm(gmm.means_, axis=1)
    pothole_component = int(np.argsort(ranking, kind="stable")[-1])
    return [int(c) == pothole_component for c in gmm.predict(z)]


def _colocated(picked, radius_m: float) -> int:
    """How many of `picked` have another of `picked` from a DIFFERENT DAY nearby."""
    hits = 0
    for a in picked:
        for b in picked:
            if a["client_id"] == b["client_id"] or a["day"] == b["day"]:
                continue
            if _metres((a["lon"], a["lat"]), (b["lon"], b["lat"])) <= radius_m:
                hits += 1
                break
    return hits


def _cross_day_colocated(rows, flags, radius_m: float) -> tuple[int, int]:
    """(pothole count, how many have a pothole from another DAY within radius)."""
    picked = [r for r, f in zip(rows, flags, strict=True) if f]
    return len(picked), _colocated(picked, radius_m)


def _null_colocation(rows, picked, radius_m: float, draws: int = 30) -> list[int]:
    """Co-location for random subsets matched to `picked`'s per-day counts.

    **Without this the headline number is meaningless.** A class holding 243 of
    4,637 points spread over 35 km of road might co-locate zero times purely
    because it is sparse, and reporting "zero" as a finding would be an artefact.
    Matching the per-day counts as well as the total also removes the other
    obvious confound -- a class concentrated on one day cannot co-locate ACROSS
    days no matter how reproducible the detector is.

    Deterministic seed so a rerun reproduces the interval.
    """
    import random

    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    counts: dict = {}
    for r in picked:
        counts[r["day"]] = counts.get(r["day"], 0) + 1

    rng = random.Random(11)
    out = []
    for _ in range(draws):
        sample = []
        for day, n in counts.items():
            pool_for_day = by_day.get(day, [])
            sample += rng.sample(pool_for_day, min(n, len(pool_for_day)))
        out.append(_colocated(sample, radius_m))
    return sorted(out)


async def run_reclassify(pool, args) -> int:
    async with pool.acquire() as conn:
        rows = [
            dict(r)
            for r in await conn.fetch(
                """
                SELECT client_id, device_id, ts_utc, magnitude, accel_std,
                       gbar_in_max, speed_mps,
                       ST_X(geom::geometry) AS lon, ST_Y(geom::geometry) AS lat
                FROM asset_observation
                WHERE magnitude IS NOT NULL AND accel_std IS NOT NULL
                  AND received_at > now() - make_interval(days => $1)
                ORDER BY ts_utc, client_id
                """,
                args.window_days,
            )
        ]
    for r in rows:
        r["day"] = r["ts_utc"].date()

    if len(rows) < _MIN_ROWS_PER_SURVEY:
        print(f"Only {len(rows)} fittable observations. Nothing to conclude.")
        return 2

    print("Read-only re-classification experiment. Nothing is written.\n")
    print(
        "Question: is the GLOBAL classifier why no pothole is ever detected twice?\n"
        "The paper fits per road segment per survey, making the class assignment\n"
        "relative; the server fits once over everything, making it absolute.\n"
    )
    print(f"{len(rows)} fittable observations, co-location radius {args.radius:.0f} m.")

    surveys: dict = {}
    for r in rows:
        surveys.setdefault((r["device_id"], r["day"]), []).append(r)
    usable = {k: v for k, v in surveys.items() if len(v) >= _MIN_ROWS_PER_SURVEY}
    print(
        f"{len(surveys)} (device, day) survey(s); {len(usable)} with "
        f">= {_MIN_ROWS_PER_SURVEY} rows and therefore fittable.\n"
    )

    header = f"{'strategy':<34} {'potholes':>9} {'cross-day co-located':>21} {'share':>7}"
    print(header)
    print("-" * len(header))

    results = []
    for label, corrected in (
        ("global GMM, shipped energy rule", False),
        ("global GMM, corrected energy", True),
    ):
        n, hits = _cross_day_colocated(
            rows, _fit_and_label(rows, corrected_energy=corrected), args.radius
        )
        results.append((label, n, hits))

    per_survey: dict[str, bool] = {}
    for group in usable.values():
        for r, f in zip(group, _fit_and_label(group, corrected_energy=True), strict=True):
            per_survey[r["client_id"]] = f
    n, hits = _cross_day_colocated(
        rows, [per_survey.get(r["client_id"], False) for r in rows], args.radius
    )
    results.append(("per-survey GMM, corrected energy", n, hits))

    for label, n, hits in results:
        share = f"{100.0 * hits / n:.1f}%" if n else "-"
        print(f"{label:<34} {n:>9} {hits:>21} {share:>7}")

    # The null. A bare zero above proves nothing on its own -- see _null_colocation.
    baseline_flags = _fit_and_label(rows, corrected_energy=True)
    picked = [r for r, f in zip(rows, baseline_flags, strict=True) if f]
    null = _null_colocation(rows, picked, args.radius)
    observed = _colocated(picked, args.radius)
    below = sum(1 for t in null if t <= observed)
    print()
    print(
        f"{'day-matched null (30 draws)':<34} {len(picked):>9} "
        f"{f'{null[0]}-{null[-1]}, median {null[len(null) // 2]}':>21}"
    )
    print(f"{'':<34} {'':>9} {f'{below}/30 draws <= observed':>21}")

    print()
    if max(h for _, _, h in results) == 0 and null[0] > 0:
        print("VERDICT: no classification strategy produces a single cross-day repeat,")
        print("         and the observed 0 is BELOW the day-matched null -- so it is not")
        print("         an artefact of sparsity. The classifier is not the cause.")
        print("         Whatever the sensor pipeline picks out is not a stable physical")
        print("         feature of the road, so no clustering, corroboration or")
        print("         classification change can help. The next step is upstream:")
        print("         the detector and its per-drive calibration, not the crowd layer.")
    elif max(h for _, _, h in results) == 0:
        print("INCONCLUSIVE: zero repeats observed, but the null also reaches zero --")
        print("              this class is too sparse for the measurement to say anything.")
    else:
        print("VERDICT: per-survey classification restores cross-day co-location.")
        print("         The global fit was the cause, and implementing the paper's")
        print("         per-survey classification is worth the schema change it needs.")
    return 0


# ── Mode 4: how big is a "single pothole", really? ─────────────────────────

_DIAMETER_SQL = """
WITH m AS (
    SELECT l.cluster_id, o.client_id, o.geom, o.accuracy_m
    FROM observation_cluster_link l
    JOIN asset_observation o ON o.client_id = l.member_id AND l.kind = 'observation'
),
pairs AS (
    SELECT a.cluster_id,
           ST_Distance(a.geom, b.geom) AS d,
           2 * GREATEST(a.accuracy_m, b.accuracy_m) AS adaptive_reach
    FROM m a JOIN m b ON a.cluster_id = b.cluster_id AND a.client_id < b.client_id
),
diam AS (
    SELECT a.cluster_id, max(ST_Distance(a.geom, b.geom)) AS d
    FROM m a JOIN m b ON a.cluster_id = b.cluster_id
    GROUP BY a.cluster_id
)
SELECT
    (SELECT count(*) FROM diam WHERE d = 0) AS single_point,
    (SELECT count(*) FROM diam WHERE d > 0 AND d <= 10) AS upto_10,
    (SELECT count(*) FROM diam WHERE d > 10 AND d <= 25) AS m10_25,
    (SELECT count(*) FROM diam WHERE d > 25 AND d <= 50) AS m25_50,
    (SELECT count(*) FROM diam WHERE d > 50) AS over_50,
    (SELECT round(max(d)::numeric, 1) FROM diam) AS widest,
    (SELECT count(*) FROM pairs) AS member_pairs,
    (SELECT count(*) FROM pairs WHERE d > adaptive_reach) AS beyond_reach
"""


async def run_diameters(pool, args) -> int:
    """Acceptance test for the assignment rule. Read-only.

    A cluster is supposed to be ONE defect. A 124 m cluster is a stretch of road
    drawn as a single marker, and it is what DBSCAN's transitive chaining produced
    (A-B, B-C, therefore A-C at any distance) on top of a flat 25 m radius that was
    ~3.7x the median 2-sigma the data actually reports.
    """
    async with pool.acquire() as conn:
        r = await conn.fetchrow(_DIAMETER_SQL)
        total = await conn.fetchval(
            "SELECT count(*) FROM asset_cluster WHERE repaired_at IS NULL"
        )

    print("Read-only. Nothing is written.\n")
    print(f"{total} unrepaired cluster(s).\n")
    print(f"{'cluster diameter':<20} {'count':>7}")
    print("-" * 28)
    for label, key in (
        ("single point", "single_point"),
        ("<= 10 m", "upto_10"),
        ("10-25 m", "m10_25"),
        ("25-50 m", "m25_50"),
        ("> 50 m", "over_50"),
    ):
        print(f"{label:<20} {r[key]:>7}")
    print(f"{'widest':<20} {str(r['widest']) + ' m':>7}")

    pairs, beyond = r["member_pairs"], r["beyond_reach"]
    share = f"{100.0 * beyond / pairs:.0f}%" if pairs else "-"
    print()
    print(f"member pairs beyond their own 2-sigma reach: {beyond}/{pairs} ({share})")
    print()
    if r["m25_50"] or r["over_50"]:
        print("Clusters exist that are wider than the assignment ceiling. That can only")
        print("happen by chaining, so a cluster here is not one defect.")
    else:
        print("No cluster exceeds the assignment ceiling — consistent with centroid")
        print("matching, which cannot chain.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sweep", action="store_true", help="Vary the clustering parameters")
    mode.add_argument(
        "--accumulate", action="store_true", help="Cluster surveys 1..k and report stability"
    )
    mode.add_argument(
        "--reclassify",
        action="store_true",
        help="Compare global vs per-survey classification by cross-day co-location",
    )
    mode.add_argument(
        "--diameters",
        action="store_true",
        help="Cluster-size histogram: is a cluster one defect, or a stretch of road?",
    )
    ap.add_argument(
        "--eps", type=float, nargs="+", default=[settings.cluster_eps_m],
        help="DBSCAN radius in metres (default: CLUSTER_EPS_M)",
    )
    ap.add_argument(
        "--min-points", type=int, nargs="+", default=[settings.cluster_min_points],
        help="DBSCAN minimum points (default: CLUSTER_MIN_POINTS)",
    )
    ap.add_argument("--window-days", type=int, default=settings.cluster_window_days)
    ap.add_argument("--min-conf", type=float, default=settings.cluster_member_min_confidence)
    ap.add_argument("--pass-gap", type=int, default=settings.cluster_pass_gap_minutes)
    ap.add_argument(
        "--radius",
        type=float,
        default=25.0,
        help="Co-location radius in metres for --reclassify (default 25, matching eps)",
    )
    args = ap.parse_args()

    pool = await create_pool()
    try:
        if args.sweep:
            await run_sweep(pool, args)
            return 0
        if args.reclassify:
            return await run_reclassify(pool, args)
        if args.diameters:
            return await run_diameters(pool, args)
        return await run_accumulate(pool, args)
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
