"""Read-path query for confirmed potholes.

Zoom-aware over the public, repair-filtered subset of asset_cluster:
  - zoom  > 14: individual clusters as `pothole` items.
  - zoom <= 14: grid-aggregated `cluster` items.

Two tiers (Phase 2.4), selected by ``detail``:
  - detail=False (public): locations only — id/lat/lon, or centroid/count.
  - detail=True  (staff) : full fields — severity, confidence, distinct_devices,
    last_seen, source, max_severity.

The same SQL drives both; only which columns are surfaced to the response model
differs. The write side (the clustering job) lives in app/fusion/service.py;
this is the read side only — no mutation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg
from fastapi import HTTPException

from app.config import settings
from app.models.potholes import (
    ClusterAggItem,
    PotholeItem,
    PotholesResponse,
    PublicClusterItem,
    PublicPotholeItem,
    PublicPotholesResponse,
)

logger = logging.getLogger(__name__)

# zoom strictly greater than this returns individual potholes; at or below it the
# server grid-aggregates to keep low-zoom views uncluttered.
INDIVIDUAL_ZOOM_THRESHOLD = 14
# Hard cap on rows returned per request, to bound payload + latency.
MAX_ITEMS = 1000

# Shared public-visibility filter. $1=min_devices $2=window_days
# $3=min_lon $4=min_lat $5=max_lon $6=max_lat $7=since (timestamptz|NULL)
_FILTER = """
    asset_type = 'pothole'
    AND repaired_at IS NULL
    AND distinct_devices >= $1
    AND last_seen >= now() - make_interval(days => $2)
    AND centroid && ST_MakeEnvelope($3, $4, $5, $6, 4326)::geography
    AND ($7::timestamptz IS NULL OR updated_at > $7)
"""

_INDIVIDUAL_SQL = f"""
SELECT
    cluster_id,
    ST_Y(centroid::geometry) AS lat,
    ST_X(centroid::geometry) AS lon,
    severity, confidence, observation_count, distinct_devices, last_seen, source
FROM asset_cluster
WHERE {_FILTER}
ORDER BY cluster_id
LIMIT $8
"""

# $8 = grid cell size in degrees (derived from zoom by the caller).
_AGGREGATE_SQL = f"""
SELECT
    avg(ST_Y(centroid::geometry)) AS centroid_lat,
    avg(ST_X(centroid::geometry)) AS centroid_lon,
    count(*)::int AS count,
    max(severity) AS max_severity
FROM asset_cluster
WHERE {_FILTER}
GROUP BY ST_SnapToGrid(centroid::geometry, $8)
ORDER BY count DESC
LIMIT {MAX_ITEMS}
"""


async def query_potholes(
    pool: asyncpg.Pool,
    *,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    zoom: int,
    since: datetime | None,
    detail: bool = False,
) -> PotholesResponse | PublicPotholesResponse:
    """Return confirmed potholes within the bbox, aggregated by zoom.

    ``detail=False`` (public) surfaces locations only; ``detail=True`` (staff)
    surfaces the full per-cluster fields.
    """
    min_devices = settings.cluster_min_distinct_devices
    window_days = settings.cluster_window_days
    generated_at = datetime.now(UTC).isoformat()

    individual = zoom > INDIVIDUAL_ZOOM_THRESHOLD

    try:
        async with pool.acquire() as conn:
            if individual:
                rows = await conn.fetch(
                    _INDIVIDUAL_SQL,
                    min_devices, window_days, min_lon, min_lat, max_lon, max_lat, since,
                    MAX_ITEMS,
                )
            else:
                # One tile-width in degrees at this zoom; coarser cells at lower zoom.
                cell_deg = 360.0 / (2 ** zoom)
                rows = await conn.fetch(
                    _AGGREGATE_SQL,
                    min_devices, window_days, min_lon, min_lat, max_lon, max_lat, since,
                    cell_deg,
                )
    except asyncpg.exceptions.PostgresError as exc:
        # A degenerate bbox can make ST_MakeEnvelope(...)::geography raise — e.g. "Antipodal
        # (180 degrees long) edge detected!" on a whole-world request. The route rejects the
        # obvious cases up front, but this endpoint is public and unauthenticated, so no geometry
        # input may reach the client as a 500. Anything PostGIS refuses to measure is a bad
        # request, not a server fault.
        logger.warning(
            "Pothole read rejected by PostGIS for bbox=%s,%s,%s,%s zoom=%s: %s",
            min_lon, min_lat, max_lon, max_lat, zoom, exc,
        )
        raise HTTPException(
            status_code=400,
            detail="bbox could not be evaluated as a geographic area; request a narrower viewport.",
        ) from exc

    items: list = []
    if individual:
        if len(rows) >= MAX_ITEMS:
            logger.warning("Pothole read hit the %d-item cap; payload truncated.", MAX_ITEMS)
        for r in rows:
            if detail:
                last_seen = r["last_seen"]
                items.append(
                    PotholeItem(
                        id=r["cluster_id"],
                        lat=r["lat"],
                        lon=r["lon"],
                        severity=r["severity"],
                        confidence=r["confidence"],
                        observation_count=r["observation_count"],
                        distinct_devices=r["distinct_devices"],
                        last_seen=last_seen.isoformat() if last_seen else None,
                        source=r["source"],
                    )
                )
            else:
                items.append(PublicPotholeItem(id=r["cluster_id"], lat=r["lat"], lon=r["lon"]))
    else:
        for r in rows:
            if detail:
                items.append(
                    ClusterAggItem(
                        centroid_lat=r["centroid_lat"],
                        centroid_lon=r["centroid_lon"],
                        count=r["count"],
                        max_severity=r["max_severity"],
                    )
                )
            else:
                items.append(
                    PublicClusterItem(
                        centroid_lat=r["centroid_lat"],
                        centroid_lon=r["centroid_lon"],
                        count=r["count"],
                    )
                )

    if detail:
        return PotholesResponse(items=items, generated_at=generated_at, next_since=generated_at)
    return PublicPotholesResponse(items=items, generated_at=generated_at, next_since=generated_at)
