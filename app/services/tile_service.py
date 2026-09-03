"""Mapbox Vector Tile generation for the operator dashboard (Phase 2.5).

Tiles are generated in PostGIS via ST_AsMVT and served straight from FastAPI
rather than from a separate tile server (Martin). The written architecture
(docs/architecture/enterprise-architecture-plan.md §3.3) calls for Martin; serving from here
instead is deliberate:

  - Martin has no authorization layer at all — it expects a reverse proxy to do
    it. Putting it in front of staff-gated data means a second deployable plus a
    proxy, versus reusing the CurrentStaff dependency that already exists.
  - The filter thresholds live in app/config.py. Martin's SQL-function config
    would have to duplicate them, where they would drift from the Python config.
  - The measured cost of doing it in Python is 1-3 ms per tile against an 80 ms
    p50 budget.

The URL shape matches Martin's, so it can be swapped in later without touching
the client.

Two filter tiers exist deliberately. The public read path
(app/services/cluster_query_service.py) hides clusters seen by fewer than
cluster_min_distinct_devices and hides repaired ones. The operator tiles must be
able to show BOTH: single-device clusters are the triage queue, and repaired
clusters drive the "repaired" toggle in the sidebar. So this module takes an
explicit TileFilter rather than reusing that module's `_FILTER`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import asyncpg
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# Web Mercator (EPSG:3857) spans this many metres edge to edge. Used to derive a
# tile's ground width at a given zoom, and from that the aggregation cell size.
_MERCATOR_SPAN_M = 40075016.686

# Slippy-map zoom beyond which tile coordinates stop being meaningful.
MAX_ZOOM = 22

LAYER_CLUSTERS = "clusters"
LAYER_OBSERVATIONS = "observations"
LAYER_FRAMES = "frames"
LAYERS = (LAYER_CLUSTERS, LAYER_OBSERVATIONS, LAYER_FRAMES)

# Tile queries must never consume the whole pool: it is shared with the ingestion
# path, and MapLibre issues 4-8 tile requests per pan. Constructed at import time,
# which is safe on 3.10+ (asyncio primitives no longer bind a loop on creation).
_tile_semaphore = asyncio.Semaphore(settings.tile_max_concurrency)


@dataclass(frozen=True)
class TileFilter:
    """Which clusters an operator wants to see. Defaults mirror the public tier."""

    asset_type: str = "pothole"
    min_devices: int = 0            # 0 = no corroboration floor (operator triage view)
    include_repaired: bool = False
    window_days: int = 0            # 0 → settings.cluster_window_days
    severity_min: float | None = None

    def resolved_window_days(self) -> int:
        return self.window_days or settings.cluster_window_days


def validate_tile_coords(z: int, x: int, y: int) -> None:
    """Reject out-of-range tile coordinates before they reach PostGIS.

    ST_TileEnvelope raises "Invalid tile x value" for out-of-range input, which
    would surface as an unhandled asyncpg error — i.e. a 500 on a bad client
    request. These are client errors, so name them as such.
    """
    if not 0 <= z <= MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"zoom must be in [0, {MAX_ZOOM}].")
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        raise HTTPException(
            status_code=400,
            detail=f"tile coordinates must be in [0, {limit - 1}] at zoom {z}.",
        )


def aggregation_cell_size_m(z: int) -> float:
    """Grid cell size in 3857 metres, giving ~tile_aggregate_bins bins per tile."""
    tile_span_m = _MERCATOR_SPAN_M / (2**z)
    return tile_span_m / settings.tile_aggregate_bins


# ── SQL ───────────────────────────────────────────────────────────────────────
# All spatial predicates are planar (3857) so they hit the functional GiST index
# added in migrations/007_tiles.sql. Filtering in geography space instead would
# match the older index but reintroduces the z0/z1 antipodal error that
# app/routes/potholes.py already had to work around.
#
# $1=z $2=x $3=y $4=asset_type $5=min_devices $6=include_repaired $7=window_days
# $8=severity_min $9=limit

_CLUSTER_TILE_SQL = f"""
WITH bounds AS (SELECT ST_TileEnvelope($1, $2, $3) AS env)
SELECT ST_AsMVT(t, '{LAYER_CLUSTERS}', {{extent}}, 'geom')
FROM (
    SELECT
        ST_AsMVTGeom(
            ST_Transform(c.centroid::geometry, 3857), b.env,
            {{extent}}, {{buffer}}, true
        ) AS geom,
        c.cluster_id,
        c.severity,
        c.confidence,
        c.observation_count,
        c.distinct_devices,
        c.distinct_passes,
        c.source,
        (c.repaired_at IS NOT NULL) AS repaired,
        extract(epoch FROM c.last_seen)::bigint AS last_seen_epoch
    FROM asset_cluster c, bounds b
    WHERE c.asset_type = $4
      AND ST_Transform(c.centroid::geometry, 3857) && b.env
      AND c.distinct_devices >= $5
      AND ($6::boolean OR c.repaired_at IS NULL)
      AND c.last_seen >= now() - make_interval(days => $7)
      AND ($8::double precision IS NULL OR c.severity >= $8)
    -- Ordered so the per-tile cap truncates deterministically. Without it, which
    -- clusters survive LIMIT is whatever the planner returns, and markers pop in
    -- and out between pans at the same zoom. Worst-first also means a truncated
    -- tile keeps the clusters an operator most needs to see.
    ORDER BY c.severity DESC NULLS LAST, c.cluster_id
    LIMIT $9
) AS t
WHERE t.geom IS NOT NULL
"""

# Same filter, grid-aggregated. Without this a low-zoom tile is unbounded: at z10
# one tile covers an entire city, so it would serve every cluster in the database.
# $10 = grid cell size in 3857 metres.
_CLUSTER_TILE_AGG_SQL = f"""
WITH bounds AS (SELECT ST_TileEnvelope($1, $2, $3) AS env),
hits AS (
    SELECT ST_Transform(c.centroid::geometry, 3857) AS g, c.severity
    FROM asset_cluster c, bounds b
    WHERE c.asset_type = $4
      AND ST_Transform(c.centroid::geometry, 3857) && b.env
      AND c.distinct_devices >= $5
      AND ($6::boolean OR c.repaired_at IS NULL)
      AND c.last_seen >= now() - make_interval(days => $7)
      AND ($8::double precision IS NULL OR c.severity >= $8)
),
binned AS (
    SELECT
        ST_Centroid(ST_Collect(g)) AS g,
        count(*)::int              AS point_count,
        max(severity)              AS max_severity
    FROM hits
    GROUP BY ST_SnapToGrid(g, $10)
    LIMIT $9
)
SELECT ST_AsMVT(t, '{LAYER_CLUSTERS}', {{extent}}, 'geom')
FROM (
    SELECT
        ST_AsMVTGeom(g, (SELECT env FROM bounds), {{extent}}, {{buffer}}, true) AS geom,
        point_count,
        max_severity
    FROM binned
) AS t
WHERE t.geom IS NOT NULL
"""

# Raw sensor observations, for street-level inspection of what fed a cluster.
# $1=z $2=x $3=y $4=asset_type $5=window_days $6=limit
_OBSERVATION_TILE_SQL = f"""
WITH bounds AS (SELECT ST_TileEnvelope($1, $2, $3) AS env)
SELECT ST_AsMVT(t, '{LAYER_OBSERVATIONS}', {{extent}}, 'geom')
FROM (
    SELECT
        ST_AsMVTGeom(
            ST_Transform(o.geom::geometry, 3857), b.env,
            {{extent}}, {{buffer}}, true
        ) AS geom,
        o.client_id,
        o.sensor_class,
        o.sensor_p_pothole,
        o.sensor_severity,
        o.sensor_is_outlier,
        o.speed_mps,
        o.accuracy_m,
        extract(epoch FROM o.ts_utc)::bigint AS ts_epoch
    FROM asset_observation o, bounds b
    WHERE o.asset_type = $4
      AND ST_Transform(o.geom::geometry, 3857) && b.env
      AND o.ts_utc >= now() - make_interval(days => $5)
    LIMIT $6
) AS t
WHERE t.geom IS NOT NULL
"""


# Camera frames as raw points — the visual half of what the pipeline saw.
#
# The observations layer answers "where did a wheel hit something?". This answers
# "where did the camera think it saw a defect?", which is a different set: 98.6%
# of pothole-classed observations have no coincident frame at all, and the frames
# outnumber the observations (5,615 vs 4,637 on the collected data).
#
# LEFT JOINed to fusion_pair because the interesting question about a camera
# detection is not its score but whether it reached fusion. A frame that scored
# 0.9 and never paired contributed nothing, and that is invisible from the score
# alone. A frame pairs with at most one observation (the pairing search takes
# ROW_NUMBER() = 1 per frame), so this cannot fan out.
#
# Deliberately NOT filtered on server_probability: the client decides what to
# show, exactly as with the observations layer. A threshold here would bake one
# operator's idea of "interesting" into the transport.
_FRAME_TILE_SQL = f"""
WITH bounds AS (SELECT ST_TileEnvelope($1, $2, $3) AS env)
SELECT ST_AsMVT(t, '{LAYER_FRAMES}', {{extent}}, 'geom')
FROM (
    SELECT
        ST_AsMVTGeom(
            ST_Transform(f.geom::geometry, 3857), b.env,
            {{extent}}, {{buffer}}, true
        ) AS geom,
        f.client_id,
        f.device_probability,
        f.server_probability,
        f.server_model_id,
        -- Box count rather than the boxes themselves: the geometry is
        -- frame-relative and means nothing in map space, but "how many did it
        -- find" is the number an operator reads.
        --
        -- Counts elements carrying a bbox, NOT jsonb_array_length(): the hybrid
        -- backend appends a "_vlm_verdict" element to this same list
        -- (app/detection/hybrid_v1.py), which has no bbox and is not a box. Length
        -- would report one box too many on every VLM-verified frame and size the
        -- map's frame markers wrong. The typeof guard keeps a malformed row from
        -- raising for the whole tile. Matches app/services/detection_boxes.py,
        -- which filters structurally for the same reason.
        COALESCE((
            SELECT count(*)
            FROM jsonb_array_elements(
                     CASE WHEN jsonb_typeof(f.server_detections) = 'array'
                          THEN f.server_detections ELSE '[]'::jsonb END) AS d
            WHERE jsonb_typeof(d -> 'bbox') = 'object'
        ), 0)::int AS server_box_count,
        (f.detected_at IS NOT NULL) AS detected,
        p.fused_confidence,
        (p.frame_client_id IS NOT NULL) AS paired,
        COALESCE(p.is_primary, false) AS is_primary,
        extract(epoch FROM f.ts_utc)::bigint AS ts_epoch
    FROM asset_frame f
    CROSS JOIN bounds b
    LEFT JOIN fusion_pair p ON p.frame_client_id = f.client_id
    WHERE ST_Transform(f.geom::geometry, 3857) && b.env
      AND f.ts_utc >= now() - make_interval(days => $4)
    ORDER BY f.server_probability DESC NULLS LAST, f.client_id
    LIMIT $5
) AS t
WHERE t.geom IS NOT NULL
"""


def _format(sql: str) -> str:
    return sql.format(extent=settings.tile_extent, buffer=settings.tile_buffer)


async def _run_tile_query(pool: asyncpg.Pool, sql: str, *args) -> bytes:
    """Execute a tile query under the concurrency cap, returning MVT bytes.

    ST_AsMVT is an aggregate: over zero rows it returns NULL, not empty bytes.
    An empty tile is normal (the operator panned somewhere with no data), so that
    becomes b"" and a 200 — a 404 would just fill the browser console with noise.
    """
    try:
        async with _tile_semaphore:
            async with pool.acquire() as conn:
                tile = await conn.fetchval(
                    sql, *args, timeout=settings.tile_query_timeout_seconds
                )
    except TimeoutError as exc:
        # Bounded by tile_query_timeout_seconds so one pathological tile cannot
        # hold a pool connection long enough to stall ingestion.
        logger.warning("Tile query timed out: %s", exc)
        raise HTTPException(
            status_code=503, detail="Tile generation timed out; try a higher zoom."
        ) from exc
    except asyncpg.exceptions.PostgresError as exc:
        # Geometry that PostGIS refuses to process is a bad request, not a server
        # fault. Mirrors the handling in cluster_query_service.query_potholes.
        logger.warning("Tile query rejected by PostGIS: %s", exc)
        raise HTTPException(
            status_code=400, detail="Tile could not be generated for these coordinates."
        ) from exc

    return bytes(tile) if tile is not None else b""


async def render_cluster_tile(
    pool: asyncpg.Pool, *, z: int, x: int, y: int, filters: TileFilter
) -> bytes:
    """Render the cluster layer, aggregating at low zoom."""
    validate_tile_coords(z, x, y)
    common = (
        z, x, y,
        filters.asset_type,
        filters.min_devices,
        filters.include_repaired,
        filters.resolved_window_days(),
        filters.severity_min,
        settings.tile_max_features,
    )
    if z <= settings.tile_aggregate_max_zoom:
        return await _run_tile_query(
            pool, _format(_CLUSTER_TILE_AGG_SQL), *common, aggregation_cell_size_m(z)
        )
    return await _run_tile_query(pool, _format(_CLUSTER_TILE_SQL), *common)


async def render_observation_tile(
    pool: asyncpg.Pool, *, z: int, x: int, y: int, filters: TileFilter
) -> bytes:
    """Render raw observation points. Street-level zooms only."""
    validate_tile_coords(z, x, y)
    if z < settings.tile_observations_min_zoom:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The observations layer requires zoom >= "
                f"{settings.tile_observations_min_zoom}."
            ),
        )
    return await _run_tile_query(
        pool,
        _format(_OBSERVATION_TILE_SQL),
        z, x, y,
        filters.asset_type,
        filters.resolved_window_days(),
        settings.tile_max_features,
    )


async def render_frame_tile(
    pool: asyncpg.Pool, *, z: int, x: int, y: int, filters: TileFilter
) -> bytes:
    """Render camera frames as raw points. Street-level zooms only.

    Same zoom floor rationale as the observations layer: there are thousands of
    these and they are individually meaningless zoomed out, so the endpoint
    refuses rather than serving a tile that would render as a smear.
    """
    validate_tile_coords(z, x, y)
    if z < settings.tile_frames_min_zoom:
        raise HTTPException(
            status_code=400,
            detail=f"The frames layer requires zoom >= {settings.tile_frames_min_zoom}.",
        )
    return await _run_tile_query(
        pool,
        _format(_FRAME_TILE_SQL),
        z, x, y,
        filters.resolved_window_days(),
        settings.tile_max_features,
    )
