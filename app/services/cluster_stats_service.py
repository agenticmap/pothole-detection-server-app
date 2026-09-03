"""Viewport statistics for the dashboard's KPI card (Phase 2.5b).

One aggregate query behind `GET /api/v1/clusters/stats`.

Why this exists rather than counting what the map already drew: the rendered
tiles are the wrong source for a number an operator might put in a report.

  - Below the aggregate zoom the tiles carry `point_count` / `max_severity` and
    no per-cluster `severity` or `repaired` at all, so every KPI would blank out
    when you zoom out — exactly when a city-wide total is most wanted.
  - `tile_buffer` makes adjacent tiles return the same cluster, so a naive count
    over rendered features double-counts along every tile seam.
  - `tile_max_features` truncates worst-severity-first, silently.

SQL has none of those problems, works at every zoom, and can reach `repair_log`
for a real "repaired this month" figure that no tile carries.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from app.config import settings

# Repaired-recently window. Fixed rather than configurable: it is a label on a
# card ("Repaired this month"), not a tuning knob, and a mismatch between the two
# would be a lie rather than a preference.
REPAIRED_WINDOW_DAYS = 30

# Guards the tiers parameter. Four is what the dashboard's ramp has; the cap is
# here so a hostile caller cannot ask for a thousand-way CASE expression.
MAX_TIERS = 8


@dataclass(frozen=True)
class StatsFilter:
    """Everything the query needs. Mirrors TileFilter's shape deliberately."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    asset_type: str
    window_days: int
    tiers: tuple[float, ...]


# The bbox is transformed into 3857 rather than compared as geography, so this
# hits the same functional GiST index the tile query uses
# (idx_asset_cluster_centroid_3857, migrations/007_tiles.sql). Comparing in
# geography space would build a different expression and fall back to a seq scan.
#
# $1=min_lon $2=min_lat $3=max_lon $4=max_lat $5=asset_type $6=window_days
# $7=tiers (ascending floors) $8=repaired_window_days
_STATS_SQL = """
WITH bounds AS (
    SELECT ST_Transform(
        ST_MakeEnvelope($1, $2, $3, $4, 4326), 3857
    ) AS env
),
visible AS (
    SELECT c.cluster_id, c.severity, c.confidence, c.repaired_at, c.source,
           c.distinct_devices, c.distinct_passes
    FROM asset_cluster c, bounds b
    WHERE c.asset_type = $5
      AND ST_Transform(c.centroid::geometry, 3857) && b.env
      AND c.last_seen >= now() - make_interval(days => $6)
)
SELECT
    count(*) FILTER (WHERE repaired_at IS NULL)                       AS open,
    count(*) FILTER (WHERE repaired_at IS NOT NULL)                   AS repaired,
    count(*) FILTER (WHERE repaired_at IS NULL AND severity IS NULL)  AS unrated,
    -- Corroborated: the SAME predicate the public read path applies
    -- (cluster_query_service._FILTER). The console had no way to show this, so it
    -- reported N open defects while the public API would have served none of them --
    -- every cluster in the collected corpus is one drive-past. Computed here rather
    -- than client-side so the two surfaces cannot drift about what counts.
    count(*) FILTER (
        WHERE repaired_at IS NULL AND (distinct_devices >= $9 OR distinct_passes >= $10)
    )                                                                 AS corroborated,
    avg(confidence) FILTER (WHERE repaired_at IS NULL)                AS mean_confidence,
    (
        SELECT count(DISTINCT r.cluster_id)
        FROM repair_log r
        JOIN visible v ON v.cluster_id = r.cluster_id
        WHERE r.action = 'repaired'
          AND r.created_at >= now() - make_interval(days => $8)
    ) AS repaired_recently,
    -- One bucket per tier floor. width_bucket(x, floors) returns 0 below the
    -- first floor and 1..n at or above it, so bucket i IS tier i (1-based) and
    -- the array comes back in the dashboard's own tier order. NULL severity
    -- matches no bucket and is reported as `unrated` above instead.
    (
        SELECT coalesce(
            array_agg(cnt ORDER BY tier_index),
            ARRAY[]::bigint[]
        )
        FROM (
            SELECT g.tier_index,
                   count(v.cluster_id) FILTER (WHERE v.repaired_at IS NULL) AS cnt
            FROM generate_series(1, array_length($7::double precision[], 1)) AS g(tier_index)
            LEFT JOIN visible v
                   ON v.severity IS NOT NULL
                  AND width_bucket(v.severity, $7::double precision[]) = g.tier_index
            GROUP BY g.tier_index
        ) buckets
    ) AS tier_counts,
    -- Detection-source mix, for the dock's source chips. Sources absent from the
    -- viewport are absent from the object rather than reported as zero: "no
    -- camera-reviewed clusters here" and "camera review contributed 0" are
    -- different claims, and only the first one is true.
    (
        SELECT coalesce(jsonb_object_agg(src, n), '{}'::jsonb)
        FROM (
            SELECT coalesce(v.source, 'unknown') AS src, count(*) AS n
            FROM visible v
            WHERE v.repaired_at IS NULL
            GROUP BY 1
        ) s
    ) AS source_counts
FROM visible
"""


async def get_cluster_stats(pool: asyncpg.Pool, f: StatsFilter) -> dict:
    row = await pool.fetchrow(
        _STATS_SQL,
        f.min_lon,
        f.min_lat,
        f.max_lon,
        f.max_lat,
        f.asset_type,
        f.window_days,
        list(f.tiers),
        REPAIRED_WINDOW_DAYS,
        settings.cluster_min_distinct_devices,
        settings.cluster_min_distinct_passes,
    )

    # A bbox containing nothing still returns a row (the aggregates are over an
    # empty set), so this is defensive rather than expected.
    if row is None:  # pragma: no cover
        counts: list[int] = [0] * len(f.tiers)
        return {
            "open": 0,
            "repaired": 0,
            "unrated": 0,
            "corroborated": 0,
            "mean_confidence": None,
            "repaired_last_30d": 0,
            "tier_counts": counts,
            "source_counts": {},
            "generated_at": datetime.now(UTC),
        }

    return {
        "open": row["open"],
        "repaired": row["repaired"],
        "unrated": row["unrated"],
        "corroborated": row["corroborated"],
        "mean_confidence": row["mean_confidence"],
        "repaired_last_30d": row["repaired_recently"],
        "tier_counts": list(row["tier_counts"] or []),
        # asyncpg hands jsonb back as a string unless a codec is registered.
        "source_counts": json.loads(row["source_counts"] or "{}"),
        "generated_at": datetime.now(UTC),
    }
