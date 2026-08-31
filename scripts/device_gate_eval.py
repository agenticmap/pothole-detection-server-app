"""Measure the corroboration floor — read-only, writes nothing.

`CLUSTER_MIN_DISTINCT_DEVICES` decides how many distinct devices a cluster needs
before `GET /api/v1/potholes` will show it. It ships as **2**, chosen a priori to
"suppress single-user noise" (roadmap §2.5), and has never been measured against
data.

Why it matters more than it looks. The floor is applied in exactly ONE place —
`app/services/cluster_query_service.py` — which gates `/potholes` and
`/potholes/detail`. It is NOT applied by the tile endpoints, `/clusters/stats`,
or `/clusters/{id}`. So the operator dashboard and the mobile app read the same
`asset_cluster` table and disagree: on the 2026-08 drives the dashboard shows 25
clusters and the app sees none, because no cluster has two devices. That is not a
bug in either surface; it is an unmeasured constant.

This script prints what each candidate floor would actually cost and buy:

  * **clusters** visible at that floor, and the share of all clusters kept
  * **observations** those clusters account for — the recall side, since a
    cluster hidden is every detection inside it hidden
  * **members** per cluster (median), because a floor that only keeps large
    clusters is trading coverage for confidence, and that should be legible
  * **single-pass** clusters: all members from one device within one hour. This
    is the noise signature the floor is meant to catch — one car, one moment, a
    speed bump. A floor that removes few of these while removing many clusters
    is not buying what it claims to.

There is no "correct" answer here, and this script deliberately does not pick
one. It reports the trade so the choice is evidenced. Run it after a collection
campaign, not before: with one driver the answer is forced.

Usage (from the repo root):

    python scripts/device_gate_eval.py
    python scripts/device_gate_eval.py --max-devices 6
    python scripts/device_gate_eval.py --bbox -79.55,43.70,-79.30,44.10

Safe against any database, including the one holding collected drive data: every
statement is a SELECT.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Run directly (`python scripts/device_gate_eval.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402

# Members from one device inside this span cannot be independent sightings: at the
# measured median speed of 13 m/s, CLUSTER_EPS_M (25 m) is 1.9 seconds of travel,
# so three triggers "within 25 m of each other" is one drive-past of one rough
# patch. 30 s is generous -- it still admits a slow crawl over the same defect.
#
# This is not a hypothetical. On the 2026-08 drives EVERY cluster spans 1-10 s
# (median 2.5 s) and none spans more than one day, so the crowd pipeline has to
# date produced zero genuinely corroborated defects. See _report's verdict block.
_SINGLE_PASS_SECONDS = 30

_SWEEP_SQL = """
WITH visible AS (
    SELECT c.cluster_id, c.observation_count, c.distinct_devices, c.severity
    FROM asset_cluster c
    WHERE c.asset_type = $1
      AND c.repaired_at IS NULL
      AND c.last_seen >= now() - make_interval(days => $2)
      AND ($3::double precision IS NULL OR (
            c.centroid && ST_MakeEnvelope($3, $4, $5, $6, 4326)::geography
          ))
),
spans AS (
    -- Time span and device count of each cluster's actual members, which
    -- asset_cluster does not store.
    SELECT l.cluster_id,
           count(DISTINCT o.device_id) AS devices,
           EXTRACT(EPOCH FROM (max(o.ts_utc) - min(o.ts_utc))) AS span_s
    FROM observation_cluster_link l
    JOIN asset_observation o ON o.client_id = l.member_id AND l.kind = 'observation'
    GROUP BY l.cluster_id
)
SELECT v.cluster_id, v.observation_count, v.distinct_devices, v.severity,
       COALESCE(s.span_s, 0) AS span_s
FROM visible v
LEFT JOIN spans s ON s.cluster_id = v.cluster_id
"""


async def _load(pool, asset_type: str, window_days: int, bbox):
    args = [asset_type, window_days]
    args.extend(bbox if bbox else [None, None, None, None])
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(_SWEEP_SQL, *args)]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _report(rows: list[dict], max_devices: int) -> None:
    total_clusters = len(rows)
    total_obs = sum(r["observation_count"] or 0 for r in rows)
    if total_clusters == 0:
        print("No clusters in scope. Nothing to sweep.")
        print("Run the clustering job first, or widen --window-days / --bbox.")
        return

    single_pass_total = sum(
        1
        for r in rows
        if (r["distinct_devices"] or 0) <= 1 and (r["span_s"] or 0) <= _SINGLE_PASS_SECONDS
    )

    print(f"Scope: {total_clusters} unrepaired cluster(s), {total_obs} observation(s).")
    print(
        f"       {single_pass_total} look like a single pass of a single device "
        f"(<= {_SINGLE_PASS_SECONDS} s, 1 device) -- the noise the floor targets."
    )
    print()
    header = (
        f"{'floor':>5}  {'clusters':>9} {'kept':>6}  {'observations':>12} {'kept':>6}  "
        f"{'median members':>14}  {'single-pass kept':>16}"
    )
    print(header)
    print("-" * len(header))

    for floor in range(1, max_devices + 1):
        kept = [r for r in rows if (r["distinct_devices"] or 0) >= floor]
        kept_obs = sum(r["observation_count"] or 0 for r in kept)
        kept_single = sum(
            1
            for r in kept
            if (r["distinct_devices"] or 0) <= 1 and (r["span_s"] or 0) <= _SINGLE_PASS_SECONDS
        )
        med = _median([float(r["observation_count"] or 0) for r in kept])
        marker = "  <- configured" if floor == settings.cluster_min_distinct_devices else ""
        print(
            f"{floor:>5}  {len(kept):>9} {100.0 * len(kept) / total_clusters:>5.1f}%  "
            f"{kept_obs:>12} {(100.0 * kept_obs / total_obs) if total_obs else 0:>5.1f}%  "
            f"{med:>14.1f}  {kept_single:>16}{marker}"
        )

    print()
    devices_present = sorted({(r["distinct_devices"] or 0) for r in rows})
    print(f"Distinct-device values present: {devices_present}")

    # Member time span is the diagnostic that matters more than the device count,
    # because it says whether a cluster is corroboration or one pass counted
    # three times. Reported as a histogram rather than a mean: the distribution
    # is what distinguishes the two, and a mean would hide a bimodal set.
    spans = sorted(float(r["span_s"] or 0) for r in rows)
    buckets = [(0, 10, "< 10 s"), (10, 30, "10-30 s"), (30, 3600, "30 s - 1 h"),
               (3600, 86400, "1 h - 1 day"), (86400, float("inf"), "> 1 day")]
    print()
    print("Member time span per cluster (is this corroboration, or one pass?):")
    for lo, hi, label in buckets:
        n = sum(1 for s in spans if lo <= s < hi)
        if n:
            print(f"  {label:>12}  {n:>4}  {100.0 * n / total_clusters:>5.1f}%")
    print(f"  median span: {_median(spans):.1f} s")

    print()
    if max(devices_present) < settings.cluster_min_distinct_devices:
        print(
            f"VERDICT: no cluster reaches the configured floor of "
            f"{settings.cluster_min_distinct_devices}, so GET /api/v1/potholes returns an "
            f"empty list for every viewport while the dashboard shows {total_clusters} "
            f"from the same table."
        )
    if single_pass_total == total_clusters:
        print(
            f"         AND all {total_clusters} are single-device single-pass. Lowering "
            f"CLUSTER_MIN_DISTINCT_DEVICES to 1 would not reveal corroborated defects -- "
            f"there are none. It would publish {total_clusters} single-pass artefacts as "
            f"confirmed potholes. The floor is currently the only thing stopping that, "
            f"which makes it load-bearing rather than merely conservative."
        )
        print(
            "         The real fix is a second vehicle, or a corroboration rule based on "
            "distinct PASSES (device, time-bucket) rather than distinct devices -- one car "
            "over the same defect on three different days is real evidence that this "
            "pipeline currently cannot express."
        )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-devices", type=int, default=4, help="Highest floor to sweep (default 4)")
    ap.add_argument("--asset-type", default="pothole")
    ap.add_argument(
        "--window-days",
        type=int,
        default=settings.cluster_window_days,
        help="Match the read path's recency filter (default: CLUSTER_WINDOW_DAYS)",
    )
    ap.add_argument("--bbox", default=None, help="minLon,minLat,maxLon,maxLat (default: all)")
    args = ap.parse_args()

    bbox = None
    if args.bbox:
        parts = [float(p) for p in args.bbox.split(",")]
        if len(parts) != 4:
            ap.error("--bbox must be 'minLon,minLat,maxLon,maxLat'")
        bbox = parts

    pool = await create_pool()
    try:
        rows = await _load(pool, args.asset_type, args.window_days, bbox)
    finally:
        await pool.close()

    _report(rows, args.max_devices)


if __name__ == "__main__":
    asyncio.run(main())
