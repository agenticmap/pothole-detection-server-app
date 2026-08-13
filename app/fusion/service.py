"""Fusion orchestration — the two scheduled jobs.

run_fit_job:   gate on accumulated observations, fit a new SensorModel, persist
               it active. (The "enough new observations and/or a closed
               bearing-segment" trigger from clusbearing.m maps, server-side, to
               the accumulation gate below; per-trip bearing segmentation is an
               on-device notion without a clean cron analogue.)
run_fusion_job: (1) score unscored observations with the active model, (2) pair
               unprocessed frames with the nearest same-device observation,
               (3) fuse, (4) write fusion_pair + fusion_run, (5) mark frames
               processed. Advisory-locked against overlap; one transaction for
               the pairing step.
"""

from __future__ import annotations

import json
import logging
import math
from uuid import uuid4

import asyncpg

from app.config import settings
from app.fusion.engine import FusionInput
from app.fusion.registry import get_engine
from app.sensor_model.fit import FitError, fit_sensor_model
from app.sensor_model.model import SensorModel, SeverityCalibration
from app.sensor_model.score import score_observation
from app.sensor_model.store import load_active_model, save_model

logger = logging.getLogger(__name__)

# Session-level advisory lock key — keeps the fusion job single-flight even
# across processes (belt-and-suspenders with APScheduler max_instances=1).
_FUSION_LOCK_KEY = 0x504F54  # 'POT'
# Distinct key for the clustering job so it can run concurrently with fusion.
_CLUSTER_LOCK_KEY = 0x504F55  # 'POT' + 1


# ── Fit job ─────────────────────────────────────────────────────────────────

_COUNT_FITTABLE_SQL = """
SELECT count(*) FROM asset_observation
WHERE magnitude IS NOT NULL AND accel_std IS NOT NULL
"""

_SELECT_FITTABLE_SQL = """
SELECT magnitude, accel_std, gbar_in_max, speed_mps
FROM asset_observation
WHERE magnitude IS NOT NULL AND accel_std IS NOT NULL
"""


async def run_fit_job(pool: asyncpg.Pool) -> str | None:
    """Refit the sensor model when enough (new) observations have accumulated.

    Returns the new model_version, or None if the gate was not met / fit failed.
    """
    async with pool.acquire() as conn:
        total = await conn.fetchval(_COUNT_FITTABLE_SQL)

    n_min = settings.sensor_fit_min_observations
    if total < n_min:
        logger.info("Fit gate not met: %d/%d fittable observations", total, n_min)
        return None

    active = await load_active_model(pool)
    if active is not None and (total - active.n_observations) < n_min:
        logger.info(
            "Fit skipped: only %d new observations since last fit (need %d)",
            total - active.n_observations, n_min,
        )
        return None

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_FITTABLE_SQL)
    rows_dicts = [dict(r) for r in rows]

    try:
        model, blob = fit_sensor_model(
            rows_dicts,
            k_default=settings.sensor_fit_k_default,
            k_max=settings.sensor_fit_k_max,
            contamination=settings.sensor_iforest_contamination,
            random_state=settings.sensor_random_state,
            severity_calib=SeverityCalibration(
                speed_ref=settings.severity_speed_ref,
                scale=settings.severity_scale,
            ),
        )
    except FitError as e:
        logger.warning("Sensor model fit failed: %s", e)
        return None

    await save_model(pool, model, blob)
    return model.model_version


# ── Fusion job ────────────────────────────────────────────────────────────────

_SELECT_UNSCORED_SQL = """
SELECT client_id, magnitude, accel_std, gbar_in_max, speed_mps
FROM asset_observation
WHERE scored_at IS NULL
ORDER BY received_at ASC, client_id ASC
LIMIT $1
"""

_UPDATE_SCORED_SQL = """
UPDATE asset_observation
SET sensor_class = $2, sensor_p_pothole = $3, sensor_severity = $4,
    sensor_is_outlier = $5, sensor_model_version = $6, scored_at = now()
WHERE client_id = $1
"""

_SELECT_FRAME_BATCH_SQL = """
SELECT client_id FROM asset_frame
WHERE processed_at IS NULL
ORDER BY received_at ASC, client_id ASC
LIMIT $1
"""

_PAIRING_SQL = """
WITH unprocessed AS (
    SELECT client_id, device_id, ts_utc, geom, device_probability, server_probability
    FROM asset_frame
    WHERE client_id = ANY($1::text[])
),
candidates AS (
    SELECT
        f.client_id AS frame_client_id,
        o.client_id AS event_client_id,
        -- Prefer the server detector's probability (Phase 2.3) when present;
        -- fall back to the on-device probability for not-yet-detected frames.
        COALESCE(f.server_probability, f.device_probability) AS visual_confidence,
        o.magnitude, o.accel_std, o.gbar_in_max, o.speed_mps,
        o.sensor_p_pothole, o.sensor_severity,
        (EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc)) * 1000)::bigint AS delta_ms,
        ST_Distance(f.geom, o.geom) AS delta_m,
        ROW_NUMBER() OVER (
            PARTITION BY f.client_id
            ORDER BY abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) ASC,
                     ST_Distance(f.geom, o.geom) ASC,
                     o.client_id ASC
        ) AS rn
    FROM unprocessed f
    JOIN asset_observation o
      ON o.device_id = f.device_id
     AND ST_DWithin(f.geom, o.geom, $2)
     AND abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) * 1000 < $3
)
SELECT * FROM candidates WHERE rn = 1
"""

_INSERT_RUN_SQL = """
INSERT INTO fusion_run (run_id, engine_version, weights_jsonb, inputs_count)
VALUES ($1, $2, $3::jsonb, $4)
"""

_UPDATE_RUN_SQL = """
UPDATE fusion_run
SET completed_at = now(), outputs_count = $2, metrics_jsonb = $3::jsonb
WHERE run_id = $1
"""

_UPSERT_PAIR_SQL = """
INSERT INTO fusion_pair (
    event_client_id, frame_client_id, fused_confidence, severity,
    delta_ms, delta_m, fusion_run_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (event_client_id, frame_client_id)
DO UPDATE SET
    fused_confidence = EXCLUDED.fused_confidence,
    severity = EXCLUDED.severity,
    delta_ms = EXCLUDED.delta_ms,
    delta_m = EXCLUDED.delta_m,
    fusion_run_id = EXCLUDED.fusion_run_id
"""

_MARK_PROCESSED_SQL = """
UPDATE asset_frame SET processed_at = now()
WHERE client_id = ANY($1::text[]) AND processed_at IS NULL
"""


async def _score_unscored(conn: asyncpg.Connection, model: SensorModel) -> int:
    """Score the unscored observation backlog with the active model."""
    rows = await conn.fetch(_SELECT_UNSCORED_SQL, settings.fusion_batch_size)
    if not rows:
        return 0
    async with conn.transaction():
        for r in rows:
            res = score_observation(
                model,
                magnitude=r["magnitude"],
                accel_std=r["accel_std"],
                gbar_in_max=r["gbar_in_max"],
                speed_mps=r["speed_mps"],
            )
            await conn.execute(
                _UPDATE_SCORED_SQL,
                r["client_id"],
                res.sensor_class,
                res.p_pothole,
                res.severity,
                res.is_outlier,
                model.model_version,
            )
    return len(rows)


async def _pair_and_fuse(conn: asyncpg.Connection, model: SensorModel | None) -> int:
    """Pair unprocessed frames with the nearest same-device observation and fuse."""
    engine = get_engine(model)
    batch = await conn.fetch(_SELECT_FRAME_BATCH_SQL, settings.fusion_batch_size)
    batch_ids = [r["client_id"] for r in batch]
    if not batch_ids:
        return 0

    run_id = uuid4().hex
    async with conn.transaction():
        await conn.execute(
            _INSERT_RUN_SQL,
            run_id,
            engine.version,
            json.dumps(engine.weights()),
            len(batch_ids),
        )

        candidates = await conn.fetch(
            _PAIRING_SQL, batch_ids, settings.fusion_window_m, settings.fusion_window_ms
        )

        n_pairs = 0
        for r in candidates:
            out = engine.fuse(
                FusionInput(
                    magnitude=r["magnitude"],
                    accel_std=r["accel_std"],
                    gbar_in_max=r["gbar_in_max"],
                    speed_mps=r["speed_mps"],
                    sensor_p_pothole=r["sensor_p_pothole"],
                    sensor_severity=r["sensor_severity"],
                    visual_confidence=r["visual_confidence"],
                    delta_ms=int(r["delta_ms"]),
                    delta_m=float(r["delta_m"]),
                )
            )
            await conn.execute(
                _UPSERT_PAIR_SQL,
                r["event_client_id"],
                r["frame_client_id"],
                out.fused_confidence,
                out.severity,
                int(r["delta_ms"]),
                float(r["delta_m"]),
                run_id,
            )
            n_pairs += 1

        await conn.execute(_MARK_PROCESSED_SQL, batch_ids)
        await conn.execute(
            _UPDATE_RUN_SQL,
            run_id,
            n_pairs,
            json.dumps(
                {
                    "frames": len(batch_ids),
                    "pairs": n_pairs,
                    "sensor_model_version": model.model_version if model else None,
                }
            ),
        )
    logger.info("Fusion run %s: %d frames, %d pairs", run_id, len(batch_ids), n_pairs)
    return n_pairs


async def run_fusion_job(pool: asyncpg.Pool) -> None:
    """Score observations, then pair frames and fuse. Single-flight via advisory lock."""
    async with pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _FUSION_LOCK_KEY)
        if not locked:
            logger.info("Fusion job already running; skipping this tick.")
            return
        try:
            model = await load_active_model(pool)
            if model is not None:
                await _score_unscored(conn, model)
            await _pair_and_fuse(conn, model)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _FUSION_LOCK_KEY)


# ── Clustering job (Phase 2.2) ────────────────────────────────────────────────
#
# Collapses many independent crowd detections of the same physical pothole into a
# single asset_cluster point. Members are pothole-classed (non-outlier)
# observations OR observations in a high-confidence fusion pair, within the
# recency window. Clustering itself is PostGIS ST_ClusterDBSCAN; the repair-safe
# cluster identity (match-or-insert) lives in Python because it is row-by-row.

# Member selection, shared by the stats and clustering queries.
# $1 = window_days (int), $2 = member_min_confidence, $3 = eps_m.
_MEMBERS_CTE = """
members AS (
    SELECT
        o.client_id,
        o.device_id,
        o.geom,
        o.ts_utc,
        COALESCE(o.sensor_severity, 0.0) AS severity,
        GREATEST(COALESCE(o.sensor_p_pothole, 0.0), COALESCE(p.max_fused, 0.0)) AS confidence
    FROM asset_observation o
    LEFT JOIN (
        SELECT event_client_id, max(fused_confidence) AS max_fused
        FROM fusion_pair
        GROUP BY event_client_id
    ) p ON p.event_client_id = o.client_id
    WHERE o.received_at > now() - make_interval(days => $1)
      AND (
            (o.sensor_class = 'pothole' AND o.sensor_is_outlier IS NOT TRUE)
            OR COALESCE(p.max_fused, 0.0) >= $2
          )
      -- Repair-safe: a member already explained by a nearby repaired cluster
      -- (older than that cluster's repaired_at) is excluded so it cannot
      -- resurrect the repaired defect. A *newer* detection in the same spot
      -- survives and forms a fresh cluster.
      AND NOT EXISTS (
            SELECT 1 FROM asset_cluster c
            WHERE c.repaired_at IS NOT NULL
              AND c.repaired_at >= o.ts_utc
              AND ST_DWithin(c.centroid, o.geom, $3)
          )
)
"""

_MEMBER_STATS_SQL = f"""
WITH {_MEMBERS_CTE}
SELECT count(*)::int AS n, avg(ST_Y(geom::geometry)) AS mean_lat
FROM members
"""

# ST_ClusterDBSCAN runs on planar geometry; we project to Web Mercator (3857) so
# eps is in meters. $4 = eps in 3857 map units (= eps_m / cos(lat), corrected for
# Mercator scale by the caller), $5 = min_points. Centroid is the
# confidence-weighted mean of member points (floored weight avoids div-by-zero).
_CLUSTER_SQL = f"""
WITH {_MEMBERS_CTE},
labeled AS (
    SELECT
        m.*,
        ST_X(m.geom::geometry) AS lon,
        ST_Y(m.geom::geometry) AS lat,
        GREATEST(m.confidence, 0.001) AS w,
        ST_ClusterDBSCAN(ST_Transform(m.geom::geometry, 3857), eps := $4, minpoints := $5)
            OVER () AS lbl
    FROM members m
)
SELECT
    ST_X(centroid) AS centroid_lon,
    ST_Y(centroid) AS centroid_lat,
    severity,
    confidence,
    observation_count,
    distinct_devices,
    last_seen,
    member_ids,
    member_confidences
FROM (
    SELECT
        ST_SetSRID(ST_MakePoint(
            SUM(lon * w) / SUM(w),
            SUM(lat * w) / SUM(w)
        ), 4326) AS centroid,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY severity) AS severity,
        avg(confidence) AS confidence,
        count(*)::int AS observation_count,
        count(DISTINCT device_id)::int AS distinct_devices,
        max(ts_utc) AS last_seen,
        array_agg(client_id ORDER BY client_id) AS member_ids,
        array_agg(confidence ORDER BY client_id) AS member_confidences,
        min(client_id) AS sort_key
    FROM labeled
    WHERE lbl IS NOT NULL
    GROUP BY lbl
) g
ORDER BY sort_key
"""

# Match a freshly-computed cluster to an existing non-repaired one by centroid
# proximity, skipping any already claimed in this run. $1=lon $2=lat $3=eps_m
# $4=already-matched ids.
_FIND_EXISTING_SQL = """
SELECT cluster_id FROM asset_cluster
WHERE repaired_at IS NULL
  AND cluster_id <> ALL($4::text[])
  AND ST_DWithin(centroid, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, $3)
ORDER BY ST_Distance(centroid, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) ASC,
         cluster_id ASC
LIMIT 1
"""

_UPDATE_CLUSTER_SQL = """
UPDATE asset_cluster SET
    centroid = ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
    severity = $4, confidence = $5, observation_count = $6,
    distinct_devices = $7, last_seen = $8, updated_at = now()
WHERE cluster_id = $1
"""

_INSERT_CLUSTER_SQL = """
INSERT INTO asset_cluster (
    cluster_id, asset_type, centroid, severity, confidence,
    observation_count, distinct_devices, last_seen, source
)
VALUES ($1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
        $4, $5, $6, $7, $8, 'crowd')
"""

_DELETE_LINKS_SQL = "DELETE FROM observation_cluster_link WHERE cluster_id = $1"

_INSERT_LINK_SQL = """
INSERT INTO observation_cluster_link (cluster_id, member_id, kind, fused_confidence)
VALUES ($1, $2, 'observation', $3)
ON CONFLICT (cluster_id, member_id, kind)
DO UPDATE SET fused_confidence = EXCLUDED.fused_confidence
"""

_INSERT_CLUSTER_RUN_SQL = """
INSERT INTO cluster_run (run_id, params_jsonb, inputs_count)
VALUES ($1, $2::jsonb, $3)
"""

_UPDATE_CLUSTER_RUN_SQL = """
UPDATE cluster_run SET completed_at = now(), outputs_count = $2 WHERE run_id = $1
"""


async def run_cluster_job(pool: asyncpg.Pool) -> int:
    """Cluster recent high-confidence detections into asset_cluster (Phase 2.2).

    Returns the number of clusters upserted this run. Single-flight via advisory
    lock; the write (cluster upserts + link rebuild + audit) is one transaction.
    """
    async with pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _CLUSTER_LOCK_KEY)
        if not locked:
            logger.info("Cluster job already running; skipping this tick.")
            return 0
        try:
            window = settings.cluster_window_days
            min_conf = settings.cluster_member_min_confidence
            eps_m = settings.cluster_eps_m
            min_points = settings.cluster_min_points

            stats = await conn.fetchrow(_MEMBER_STATS_SQL, window, min_conf, eps_m)
            n_members = stats["n"] or 0
            if n_members < min_points:
                logger.info("Cluster gate not met: %d members (< %d).", n_members, min_points)
                return 0

            # Web-Mercator distorts distance by 1/cos(lat); scale eps so the
            # planar threshold corresponds to eps_m ground meters at this latitude.
            mean_lat = float(stats["mean_lat"])
            eps_units = eps_m / max(math.cos(math.radians(mean_lat)), 1e-6)

            clusters = await conn.fetch(
                _CLUSTER_SQL, window, min_conf, eps_m, eps_units, min_points
            )

            run_id = uuid4().hex
            matched: list[str] = []
            async with conn.transaction():
                await conn.execute(
                    _INSERT_CLUSTER_RUN_SQL,
                    run_id,
                    json.dumps(
                        {
                            "eps_m": eps_m,
                            "eps_units_3857": eps_units,
                            "min_points": min_points,
                            "window_days": window,
                            "member_min_confidence": min_conf,
                            "mean_lat": mean_lat,
                        }
                    ),
                    n_members,
                )

                for c in clusters:
                    lon, lat = float(c["centroid_lon"]), float(c["centroid_lat"])
                    existing = await conn.fetchval(
                        _FIND_EXISTING_SQL, lon, lat, eps_m, matched
                    )
                    if existing is not None:
                        cluster_id = existing
                        await conn.execute(
                            _UPDATE_CLUSTER_SQL, cluster_id, lon, lat,
                            c["severity"], c["confidence"], c["observation_count"],
                            c["distinct_devices"], c["last_seen"],
                        )
                    else:
                        cluster_id = "clu_" + uuid4().hex
                        await conn.execute(
                            _INSERT_CLUSTER_SQL, cluster_id, lon, lat,
                            c["severity"], c["confidence"], c["observation_count"],
                            c["distinct_devices"], c["last_seen"],
                        )
                    matched.append(cluster_id)

                    await conn.execute(_DELETE_LINKS_SQL, cluster_id)
                    for mid, mc in zip(c["member_ids"], c["member_confidences"]):
                        await conn.execute(_INSERT_LINK_SQL, cluster_id, mid, mc)

                await conn.execute(_UPDATE_CLUSTER_RUN_SQL, run_id, len(clusters))
            logger.info(
                "Cluster run %s: %d members → %d clusters.", run_id, n_members, len(clusters)
            )
            return len(clusters)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _CLUSTER_LOCK_KEY)
