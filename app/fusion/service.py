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
from app.fusion.spatiotemporal import integrate_cluster
from app.sensor_model.fit import FitError, fit_sensor_model
from app.sensor_model.model import CLASS_POTHOLE, SensorModel, SeverityCalibration
from app.sensor_model.score import score_observation
from app.sensor_model.store import load_active_model, save_model

logger = logging.getLogger(__name__)

# Session-level advisory lock key — keeps the fusion job single-flight even
# across processes (belt-and-suspenders with APScheduler max_instances=1).
_FUSION_LOCK_KEY = 0x504F54  # 'POT'
# Distinct key for the clustering job so it can run concurrently with fusion.
_CLUSTER_LOCK_KEY = 0x504F55  # 'POT' + 1
# Distinct key for the fit job. 0x504F56 belongs to the detection worker
# (app/detection/service.py), so the fit job takes the next one.
_FIT_LOCK_KEY = 0x504F57  # 'POT' + 3


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

    Single-flight via advisory lock, like the fusion and cluster jobs. Without it
    two uvicorn workers could fit concurrently and race on the
    idx_sensor_model_active partial unique index, since save_model flips which row
    is active. The lock is held on its own connection for the whole job because
    load_active_model/save_model take the pool rather than a connection.
    """
    async with pool.acquire() as lock_conn:
        locked = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", _FIT_LOCK_KEY)
        if not locked:
            logger.info("Fit job already running; skipping this tick.")
            return None
        try:
            return await _run_fit_locked(pool)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _FIT_LOCK_KEY)


async def _run_fit_locked(pool: asyncpg.Pool) -> str | None:
    """The fit itself. Caller holds _FIT_LOCK_KEY."""
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
    sensor_is_outlier = $5, sensor_model_version = $6, scored_at = now(),
    sensor_class_probs = $7::jsonb
WHERE client_id = $1
"""

_SELECT_FRAME_BATCH_SQL = """
SELECT client_id FROM asset_frame
WHERE processed_at IS NULL
ORDER BY received_at ASC, client_id ASC
LIMIT $1
"""

# Columns every candidate carries, shared by both rankings so the two cannot drift.
_CANDIDATE_COLUMNS = """
        f.client_id AS frame_client_id,
        o.client_id AS event_client_id,
        -- Prefer the server detector's probability (Phase 2.3) when present;
        -- fall back to the on-device probability for not-yet-detected frames.
        COALESCE(f.server_probability, f.device_probability) AS visual_confidence,
        o.magnitude, o.accel_std, o.gbar_in_max, o.speed_mps,
        o.sensor_p_pothole, o.sensor_severity,
        EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc)) AS delta_s,
        ST_Distance(f.geom, o.geom) AS delta_m
"""

# Lookahead-aware pairing (Phase 2.2d). The pre-2.2d ranking minimised |delta_t|
# then distance, i.e. its ideal match was a frame taken at the same instant and the
# same place as the wheel impact. That is geometrically backwards: the camera
# resolves a pothole while it is still AHEAD of the vehicle, so the frame that
# actually SAW it was taken earlier and some metres short. Re-ranking pothole_db's
# existing candidate set under the cost below changed the winner for 713 of 2197
# frames (32.5%).
#
# Three terms:
#   lead_penalty  metres by which delta_m falls outside the camera's usable
#                 ground-distance band. Zero inside the band -- the band is a
#                 plateau, not a target, because any distance within it is a
#                 legitimate view.
#   kinematic     seconds of disagreement between the observed time offset and the
#                 one implied by driving delta_m at this speed. The expected offset
#                 is -delta_m/speed (negative = frame first), so the residual is
#                 |delta_s + delta_m/speed|. This is what rejects a candidate 30 m
#                 away and 0 s apart: no vehicle covers 30 m instantly.
#   forward       flat charge for a frame taken AFTER the event. Admissible, since
#                 GPS and clock noise straddle zero, but it should lose to any
#                 backward candidate.
#
# The temporal gate is DERIVED from speed rather than fixed. At the measured median
# 13.02 m/s, 3000 ms of travel is 39 m and 75 m at p90 -- so a fixed 3000 ms and a
# fixed 25 m contradicted each other, one or the other always being slack.
# $1 = frame ids, $2 = window_m, $3 = window_ms_max, $4 = w_lead, $5 = lead_near,
# $6 = lead_far, $7 = w_kinematic, $8 = speed_floor, $9 = forward_penalty.
_PAIRING_SQL = f"""
WITH unprocessed AS (
    SELECT client_id, device_id, ts_utc, geom, device_probability, server_probability
    FROM asset_frame
    WHERE client_id = ANY($1::text[])
),
raw AS (
    SELECT
{_CANDIDATE_COLUMNS},
        GREATEST(COALESCE(o.speed_mps, 0.0), $8) AS speed
    FROM unprocessed f
    JOIN asset_observation o
      ON o.device_id = f.device_id
     AND ST_DWithin(f.geom, o.geom, $2)
     AND abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) * 1000
         < LEAST(
               $3::double precision,
               1000.0 * $2 / GREATEST(COALESCE(o.speed_mps, 0.0), $8)
           )
),
candidates AS (
    SELECT
        raw.*,
        (delta_s * 1000)::bigint AS delta_ms,
        (
            $4 * (GREATEST(0.0, $5 - delta_m) + GREATEST(0.0, delta_m - $6))
          + $7 * abs(delta_s + delta_m / speed)
          + CASE WHEN delta_s > 0 THEN $9 ELSE 0.0 END
        ) AS match_cost
    FROM raw
),
ranked AS (
    SELECT
        candidates.*,
        ROW_NUMBER() OVER (
            PARTITION BY frame_client_id
            -- event_client_id breaks ties deterministically; the repo's
            -- byte-identical-rerun property depends on it.
            ORDER BY match_cost ASC, event_client_id ASC
        ) AS rn
    FROM candidates
)
SELECT * FROM ranked WHERE rn = 1
"""

# The pre-2.2d ranking, reachable via FUSION_PAIRING_COST_ENABLED=false so the change
# can be A/B'd and reverted without a deploy. Kept as a separate statement rather
# than a parameterisation of the above, because the two differ in their window
# predicate as well as their ORDER BY, and folding them together would make both
# harder to read than either is apart.
# $1 = frame ids, $2 = window_m, $3 = window_ms (fixed).
_PAIRING_LEGACY_SQL = f"""
WITH unprocessed AS (
    SELECT client_id, device_id, ts_utc, geom, device_probability, server_probability
    FROM asset_frame
    WHERE client_id = ANY($1::text[])
),
candidates AS (
    SELECT
{_CANDIDATE_COLUMNS},
        (EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc)) * 1000)::bigint AS delta_ms,
        NULL::double precision AS match_cost,
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
    delta_ms, delta_m, fusion_run_id, match_cost
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (event_client_id, frame_client_id)
DO UPDATE SET
    fused_confidence = EXCLUDED.fused_confidence,
    severity = EXCLUDED.severity,
    delta_ms = EXCLUDED.delta_ms,
    delta_m = EXCLUDED.delta_m,
    fusion_run_id = EXCLUDED.fusion_run_id,
    match_cost = EXCLUDED.match_cost
"""

# is_primary is a per-OBSERVATION fact, but pairing runs per FRAME batch, so a later
# batch can turn up a better frame for an already-paired observation. Both statements
# therefore scan every pair of the touched observations, not just this batch's rows.
#
# Two statements rather than one, because idx_fusion_pair_primary is a partial unique
# index and Postgres enforces it row by row: a single UPDATE that promoted the new
# winner before demoting the old one would transiently hold two primaries and abort.
# A partial index cannot be a DEFERRABLE constraint, so demote-then-promote inside
# one transaction is the way to satisfy it.
_DEMOTE_PRIMARY_SQL = """
UPDATE fusion_pair SET is_primary = FALSE
WHERE event_client_id = ANY($1::text[]) AND is_primary
"""

_PROMOTE_PRIMARY_SQL = """
WITH ranked AS (
    SELECT event_client_id, frame_client_id,
           ROW_NUMBER() OVER (
               PARTITION BY event_client_id
               -- NULLS LAST so a pre-2.2d pair (no cost) never outranks a scored
               -- one; frame_client_id keeps it deterministic.
               ORDER BY match_cost ASC NULLS LAST, frame_client_id ASC
           ) AS rn
    FROM fusion_pair
    WHERE event_client_id = ANY($1::text[])
)
UPDATE fusion_pair p SET is_primary = TRUE
FROM ranked r
WHERE p.event_client_id = r.event_client_id
  AND p.frame_client_id = r.frame_client_id
  AND r.rn = 1
"""

# A frame is retired once it has paired, or once it is old enough that no partner
# event is still plausibly in flight. Marking every frame regardless -- the pre-2.2d
# behaviour -- means a frame whose event uploads later is never revisited, because
# _SELECT_FRAME_BATCH_SQL only reads processed_at IS NULL. In pothole_db 450
# candidate pairs have the event arriving after the frame, though 0 frames were
# actually lost, because the 5-minute cadence absorbed the gap. This bounds the
# exposure rather than relying on that. A grace of 0 restores the old behaviour.
# $2 = fusion_retry_grace_minutes.
# Clear this batch's frames of any pair they already have, and report which
# observations that disturbed.
#
# Required by the re-fuse path, which is how Phase 2.2d gets activated on existing
# data (UPDATE asset_frame SET processed_at = NULL). fusion_pair is keyed
# (event_client_id, frame_client_id), so the upsert can only overwrite a pair whose
# BOTH ends are unchanged. The whole point of the new ranking is that ~37% of frames
# choose a different observation -- and each of those would leave its old row behind
# under a different event_client_id, so the table would grow a second, contradictory
# population instead of being corrected. The frame is the unit of work, so the frame
# is what gets cleared.
#
# RETURNING feeds the is_primary recompute: an observation that just lost a pair may
# have lost its primary with it, and it will not appear in the new candidate set.
_DELETE_PAIRS_FOR_FRAMES_SQL = """
DELETE FROM fusion_pair
WHERE frame_client_id = ANY($1::text[])
RETURNING event_client_id
"""

_MARK_PROCESSED_SQL = """
UPDATE asset_frame f SET processed_at = now()
WHERE f.client_id = ANY($1::text[])
  AND f.processed_at IS NULL
  AND (
        EXISTS (SELECT 1 FROM fusion_pair p WHERE p.frame_client_id = f.client_id)
        OR f.received_at <= now() - make_interval(mins => $2)
      )
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
                # The full posterior, for Phase 2.2c's spatiotemporal integration.
                # NULL rather than '{}' when degenerate, so "never scored with a
                # posterior" and "posterior was empty" stay distinguishable.
                json.dumps(res.class_probs) if res.class_probs else None,
            )
    return len(rows)


async def _pair_and_fuse(conn: asyncpg.Connection, model: SensorModel | None) -> int:
    """Pair unprocessed frames with the best-matching same-device observation and fuse.

    "Best" is the lowest-cost candidate under the lookahead model (Phase 2.2d), not
    the nearest in time: the camera resolves a pothole while it is still ahead of the
    vehicle, so the frame that saw it was taken earlier and some metres short.
    """
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

        if settings.fusion_pairing_cost_enabled:
            candidates = await conn.fetch(
                _PAIRING_SQL,
                batch_ids,
                settings.fusion_window_m,
                settings.fusion_window_ms_max,
                settings.fusion_w_lead,
                settings.fusion_lead_near_m,
                settings.fusion_lead_far_m,
                settings.fusion_w_kinematic,
                settings.fusion_speed_floor_mps,
                settings.fusion_forward_penalty,
            )
        else:
            candidates = await conn.fetch(
                _PAIRING_LEGACY_SQL,
                batch_ids,
                settings.fusion_window_m,
                settings.fusion_window_ms_max,
            )

        # Drop any pair these frames already had before writing the new choice,
        # so a re-fuse corrects the table instead of appending to it.
        displaced = await conn.fetch(_DELETE_PAIRS_FOR_FRAMES_SQL, batch_ids)

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
            cost = r["match_cost"]
            await conn.execute(
                _UPSERT_PAIR_SQL,
                r["event_client_id"],
                r["frame_client_id"],
                out.fused_confidence,
                out.severity,
                int(r["delta_ms"]),
                float(r["delta_m"]),
                run_id,
                float(cost) if cost is not None else None,
            )
            n_pairs += 1

        # Recompute which frame is the primary view of each observation this batch
        # touched. Done after the upserts, over ALL of those observations' pairs
        # rather than just this batch's, because a better frame can arrive later.
        # Observations that gained a pair, plus those that merely lost one: both
        # need their primary recomputed, and only the first group is in `candidates`.
        touched = sorted(
            {r["event_client_id"] for r in candidates}
            | {r["event_client_id"] for r in displaced}
        )
        if touched:
            await conn.execute(_DEMOTE_PRIMARY_SQL, touched)
            await conn.execute(_PROMOTE_PRIMARY_SQL, touched)

        await conn.execute(
            _MARK_PROCESSED_SQL, batch_ids, settings.fusion_retry_grace_minutes
        )
        await conn.execute(
            _UPDATE_RUN_SQL,
            run_id,
            n_pairs,
            json.dumps(
                {
                    "frames": len(batch_ids),
                    "pairs": n_pairs,
                    "sensor_model_version": model.model_version if model else None,
                    # Unpaired frames inside the retry grace stay unprocessed and come
                    # back next tick, so frames > pairs does not imply frames were
                    # discarded. Record the ranking too: without it a re-fused table
                    # is two incomparable populations with no way to tell which rows
                    # came from which.
                    "pairing_cost_enabled": settings.fusion_pairing_cost_enabled,
                    "retry_grace_minutes": settings.fusion_retry_grace_minutes,
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
# $1 = window_days (int), $2 = member_min_confidence, $3 = eps_m,
# $4 = frame_only_enabled (bool), $5 = frame_only_min_probability.
_MEMBERS_CTE = """
paired AS (
    SELECT
        event_client_id,
        -- The geometrically best VIEW of this pothole, not the most agreeable
        -- VERDICT about it. One observation can win many frames (in pothole_db,
        -- 1842 pairs covered 472 observations, one of them 19 times), and a plain
        -- max() over N correlated views of the same pavement cherry-picks the
        -- friendliest -- measured at +0.148 on the visual term across 346
        -- multi-frame events, a bias that grows with N. COALESCE falls back to the
        -- old max() for pairs written before Phase 2.2d, which have no primary.
        COALESCE(
            max(fused_confidence) FILTER (WHERE is_primary),
            max(fused_confidence)
        ) AS max_fused
    FROM fusion_pair
    GROUP BY event_client_id
),
members AS (
    SELECT
        o.client_id,
        o.device_id,
        o.geom,
        o.ts_utc,
        COALESCE(o.sensor_severity, 0.0) AS severity,
        o.bearing_deg,
        o.sensor_class_probs,
        GREATEST(COALESCE(o.sensor_p_pothole, 0.0), COALESCE(p.max_fused, 0.0)) AS confidence,
        'observation' AS kind
    FROM asset_observation o
    LEFT JOIN paired p ON p.event_client_id = o.client_id
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

    UNION ALL

    -- Frame-only members (Phase 2.2d): a pothole the camera saw that no wheel hit.
    -- 98.6% of pothole-classed observations have no coincident frame at all, so
    -- vision-without-impact is the largest recall ceiling in the pipeline -- and
    -- until Phase 2.7 ships a model this arm is DISABLED, because server_probability
    -- is NULL on every frame and the only remaining input is the on-device score,
    -- whose confidence floor was dropped to ~5% mid-collection (p50 0.118). $4 is
    -- the kill switch rather than a Python branch so both member queries stay one
    -- statement each and cannot diverge.
    SELECT
        fr.client_id,
        fr.device_id,
        fr.geom,
        fr.ts_utc,
        -- A frame carries no accelerometer magnitude, so it has no severity proxy
        -- and no heading. 0.0 rather than NULL because the cluster severity is a
        -- median over this column; NULL would make the median silently ignore the
        -- member instead of counting it as unrated.
        0.0 AS severity,
        NULL::double precision AS bearing_deg,
        NULL::jsonb AS sensor_class_probs,
        COALESCE(fr.server_probability, fr.device_probability) AS confidence,
        'frame' AS kind
    FROM asset_frame fr
    WHERE $4
      AND fr.received_at > now() - make_interval(days => $1)
      AND COALESCE(fr.server_probability, fr.device_probability) >= $5
      -- Only frames that never paired: a paired frame's evidence already reached
      -- clustering through its observation's fused_confidence, and admitting it
      -- again would double-count one sighting as two members.
      AND NOT EXISTS (
            SELECT 1 FROM fusion_pair fp WHERE fp.frame_client_id = fr.client_id
          )
      AND NOT EXISTS (
            SELECT 1 FROM asset_cluster c
            WHERE c.repaired_at IS NOT NULL
              AND c.repaired_at >= fr.ts_utc
              AND ST_DWithin(c.centroid, fr.geom, $3)
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
        ST_ClusterDBSCAN(ST_Transform(m.geom::geometry, 3857), eps := $6, minpoints := $7)
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
    -- Normalise into [0, 360). The extra branch is not pedantry: atan2 returns
    -- ~-1e-15 for a mean heading of due north, and -1e-15 + 360 is exactly 360.0 in
    -- float, so a naive wrap stores 360 degrees in a column documented as [0, 360).
    CASE
        WHEN bearing_raw < -1e-9 THEN bearing_raw + 360.0
        WHEN bearing_raw < 0 THEN 0.0
        ELSE bearing_raw
    END AS bearing_deg,
    member_ids,
    member_confidences,
    member_lons,
    member_lats,
    member_ts,
    member_class_probs,
    member_bearings,
    member_devices,
    member_severities,
    member_kinds
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
        -- Circular mean, NOT avg(bearing_deg): averaging 350 and 10 arithmetically
        -- gives 180, i.e. the exact opposite heading, which would then merge the two
        -- carriageways this column exists to keep apart. Left in atan2's native
        -- (-180, 180] range here and normalised in the outer select, because mod()
        -- has no double-precision overload and an alias is not visible in its own
        -- SELECT list.
        degrees(atan2(avg(sin(radians(bearing_deg))), avg(cos(radians(bearing_deg)))))
            AS bearing_raw,
        array_agg(client_id ORDER BY client_id) AS member_ids,
        array_agg(confidence ORDER BY client_id) AS member_confidences,
        -- Per-member inputs for the spatiotemporal integration (Phase 2.2c). Ordered
        -- by client_id so every array lines up and the result does not depend on the
        -- order the planner happened to emit rows in.
        array_agg(lon ORDER BY client_id) AS member_lons,
        array_agg(lat ORDER BY client_id) AS member_lats,
        array_agg(ts_utc ORDER BY client_id) AS member_ts,
        array_agg(sensor_class_probs ORDER BY client_id) AS member_class_probs,
        array_agg(bearing_deg ORDER BY client_id) AS member_bearings,
        array_agg(device_id ORDER BY client_id) AS member_devices,
        array_agg(severity ORDER BY client_id) AS member_severities,
        -- 'observation' or 'frame'; observation_cluster_link.kind needs it per row.
        array_agg(kind ORDER BY client_id) AS member_kinds,
        min(client_id) AS sort_key
    FROM labeled
    WHERE lbl IS NOT NULL
    GROUP BY lbl
) g
ORDER BY sort_key
"""

# Match a freshly-computed cluster to an existing non-repaired one by centroid
# proximity, skipping any already claimed in this run. $1=lon $2=lat $3=eps_m
# $4=already-matched ids, $5=this group's mean bearing (NULL to disable the check),
# $6=bearing tolerance in degrees.
#
# The bearing check is §4.4 of the crowdsourcing method: two carriageways of the same
# road are within eps_m of each other, so proximity alone merges opposing traffic into
# one defect. Direction separates them.
#
# The comparison has to be circular — 350° and 10° are 20° apart, not 340° — hence the
# double modulo, which maps any difference into [0, 180]. NULL on either side means
# "unknown heading", which matches anything rather than blocking a merge; that keeps
# clusters written before migration 011 mergeable.
_FIND_EXISTING_SQL = """
SELECT cluster_id FROM asset_cluster
WHERE repaired_at IS NULL
  AND cluster_id <> ALL($4::text[])
  AND ST_DWithin(centroid, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, $3)
  AND (
        $5::double precision IS NULL
        OR bearing_deg IS NULL
        OR LEAST(
             abs(bearing_deg - $5::double precision),
             360.0 - abs(bearing_deg - $5::double precision)
           ) <= $6::double precision
      )
ORDER BY ST_Distance(centroid, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) ASC,
         cluster_id ASC
LIMIT 1
"""

_UPDATE_CLUSTER_SQL = """
UPDATE asset_cluster SET
    centroid = ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
    severity = $4, confidence = $5, observation_count = $6,
    distinct_devices = $7, last_seen = $8, updated_at = now(),
    bearing_deg = $9, class_probs = $10::jsonb
WHERE cluster_id = $1
"""

_INSERT_CLUSTER_SQL = """
INSERT INTO asset_cluster (
    cluster_id, asset_type, centroid, severity, confidence,
    observation_count, distinct_devices, last_seen, source,
    bearing_deg, class_probs
)
VALUES ($1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
        $4, $5, $6, $7, $8, 'crowd', $9, $10::jsonb)
"""

_DELETE_LINKS_SQL = "DELETE FROM observation_cluster_link WHERE cluster_id = $1"

# kind is a parameter rather than the literal 'observation' it used to be, because
# a cluster can now mix wheel-impact members with camera-only ones (Phase 2.2d).
# The CHECK constraint from migration 001 already allowed both values.
_INSERT_LINK_SQL = """
INSERT INTO observation_cluster_link (cluster_id, member_id, kind, fused_confidence)
VALUES ($1, $2, $4, $3)
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

# Guard against resurrecting a defect that was repaired *while this run was
# computing*. _MEMBERS_CTE filters out members already explained by a repair, but
# it is evaluated by _CLUSTER_SQL before the write transaction opens, so a repair
# committed in between leaves those members in the computed result while
# _FIND_EXISTING_SQL (WHERE repaired_at IS NULL) no longer matches the cluster
# they came from — and the row would be re-inserted as a brand new, un-repaired
# cluster containing exactly the observations an operator just closed out.
#
# Checking at insert time closes that window regardless of statement ordering
# (READ COMMITTED re-snapshots per statement, so simply widening the transaction
# would not). It also guards against device clock skew: repaired_at is server
# time while last_seen derives from the device-supplied ts_utc.
# $1=lon $2=lat $3=eps_m $4=last_seen
_FIND_REPAIRED_COVERING_SQL = """
SELECT cluster_id FROM asset_cluster
WHERE repaired_at IS NOT NULL
  AND repaired_at >= $4
  AND ST_DWithin(centroid, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, $3)
LIMIT 1
"""




def _circular_diff(a: float, b: float) -> float:
    """Smallest angle between two headings, in [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _split_by_direction(cluster, tolerance_deg: float) -> list[list[int]]:
    """Split one spatial group's member indices into direction sub-groups (§4.4).

    DBSCAN is direction-blind, so two carriageways of the same road -- within eps_m of
    each other but travelled in opposite directions -- land in one group. They are
    separate defects. Filtering by bearing when matching the group to an existing
    cluster cannot fix that: by then the group already spans both directions, and its
    circular mean heading is the meaningless bisector (0 and 180 average to 90).

    Single-linkage over circular distance, which is why this is done here rather than
    by partitioning DBSCAN on a fixed heading sector: fixed sectors split any road
    lying on a sector boundary, so 350 and 10 -- 20 degrees apart -- would become two
    clusters. Linkage has no boundary.

    Members with no heading carry no direction information; they join the first
    sub-group rather than being dropped or forming a spurious one of their own.
    """
    bearings = cluster["member_bearings"]
    n = len(bearings)
    known = [i for i in range(n) if bearings[i] is not None]
    unknown = [i for i in range(n) if bearings[i] is None]
    if not known:
        return [list(range(n))]

    ordered = sorted(known, key=lambda i: float(bearings[i]))
    groups: list[list[int]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        if _circular_diff(float(bearings[prev]), float(bearings[cur])) <= tolerance_deg:
            groups[-1].append(cur)
        else:
            groups.append([cur])

    # Close the circle: the lowest and highest headings may themselves be adjacent
    # (359 and 1), in which case the first and last groups are one group.
    if (
        len(groups) > 1
        and _circular_diff(float(bearings[ordered[0]]), float(bearings[ordered[-1]]))
        <= tolerance_deg
    ):
        groups[0] = groups[-1] + groups[0]
        groups.pop()

    groups[0].extend(unknown)
    return [sorted(g) for g in groups]


def _subgroup_row(cluster, idx: list[int]) -> dict:
    """Recompute a cluster row from a subset of its members.

    Mirrors the aggregate in _CLUSTER_SQL exactly: confidence-weighted centroid,
    median severity, circular-mean bearing. Only reached when a group actually spans
    more than one direction, so the common single-direction path keeps using the
    values Postgres computed.
    """
    lons = [float(cluster["member_lons"][i]) for i in idx]
    lats = [float(cluster["member_lats"][i]) for i in idx]
    confs = [float(cluster["member_confidences"][i]) for i in idx]
    sevs = sorted(float(cluster["member_severities"][i]) for i in idx)
    bearings = [
        cluster["member_bearings"][i]
        for i in idx
        if cluster["member_bearings"][i] is not None
    ]

    weights = [max(c, 0.001) for c in confs]
    total_w = sum(weights)
    mid = len(sevs) // 2
    severity = sevs[mid] if len(sevs) % 2 else (sevs[mid - 1] + sevs[mid]) / 2.0

    if bearings:
        sin_sum = sum(math.sin(math.radians(float(b))) for b in bearings) / len(bearings)
        cos_sum = sum(math.cos(math.radians(float(b))) for b in bearings) / len(bearings)
        bearing = math.degrees(math.atan2(sin_sum, cos_sum))
        if bearing < 0.0:
            bearing += 360.0
        if bearing >= 360.0:  # see the SQL note: -1e-15 + 360 == 360.0 exactly
            bearing = 0.0
    else:
        bearing = None

    return {
        "centroid_lon": sum(lo * w for lo, w in zip(lons, weights)) / total_w,
        "centroid_lat": sum(la * w for la, w in zip(lats, weights)) / total_w,
        "severity": severity,
        "confidence": sum(confs) / len(confs),
        "observation_count": len(idx),
        "distinct_devices": len({cluster["member_devices"][i] for i in idx}),
        "last_seen": max(cluster["member_ts"][i] for i in idx),
        "bearing_deg": bearing,
        "member_ids": [cluster["member_ids"][i] for i in idx],
        "member_confidences": confs,
        "member_lons": lons,
        "member_lats": lats,
        "member_ts": [cluster["member_ts"][i] for i in idx],
        "member_class_probs": [cluster["member_class_probs"][i] for i in idx],
        # Carried so a subgroup row is interchangeable with a Postgres-aggregated
        # one. member_kinds is required by the link write; the other two were
        # previously dropped, which left a split subgroup a KeyError away from any
        # future caller that re-read them.
        "member_kinds": [cluster["member_kinds"][i] for i in idx],
        "member_devices": [cluster["member_devices"][i] for i in idx],
        "member_severities": [cluster["member_severities"][i] for i in idx],
        "member_bearings": [cluster["member_bearings"][i] for i in idx],
    }


def _direction_split_clusters(clusters, tolerance_deg: float) -> list:
    """Expand DBSCAN groups into per-direction cluster candidates."""
    out = []
    for c in clusters:
        idx_groups = _split_by_direction(c, tolerance_deg)
        if len(idx_groups) == 1:
            out.append(c)  # untouched: Postgres already aggregated it correctly
            continue
        logger.info(
            "Spatial group of %d members spans %d directions; splitting.",
            c["observation_count"], len(idx_groups),
        )
        out.extend(_subgroup_row(c, idx) for idx in idx_groups)
    return out


# ── Spatiotemporal integration (Phase 2.2c) ───────────────────────────────────

# Metres per degree of latitude. Good to ~0.5% over a city, and cluster members are
# within tens of metres of each other, so a full geodesic call per member would be
# precision nobody can use.
_M_PER_DEG_LAT = 111_320.0


def _member_distances(cluster) -> tuple[list[float], list[float]]:
    """Per-member distance to the centroid (m) and age relative to the newest (s)."""
    c_lon = float(cluster["centroid_lon"])
    c_lat = float(cluster["centroid_lat"])
    lat_scale = math.cos(math.radians(c_lat))

    spatial: list[float] = []
    for lon, lat in zip(cluster["member_lons"], cluster["member_lats"]):
        dx = (float(lon) - c_lon) * _M_PER_DEG_LAT * lat_scale
        dy = (float(lat) - c_lat) * _M_PER_DEG_LAT
        spatial.append(math.hypot(dx, dy))

    newest = max(cluster["member_ts"])
    temporal = [abs((newest - t).total_seconds()) for t in cluster["member_ts"]]
    return spatial, temporal


def _integrate_cluster_row(cluster) -> tuple[float, str, list[float]] | None:
    """Spatiotemporally integrate one cluster's members (paper §4.5).

    Returns (confidence, class_probs_json, member_weights), or None when the members
    cannot support it — which is the normal case for observations scored before
    migration 011 added sensor_class_probs. The caller then keeps the legacy mean, so
    enabling this on a database of un-rescored rows changes nothing rather than
    silently producing a distribution from invented inputs.
    """
    raw = cluster["member_class_probs"]
    if raw is None or any(x is None for x in raw):
        return None

    dists = [json.loads(x) if isinstance(x, str) else x for x in raw]
    # Union of labels, stably ordered, so the vector is comparable across clusters
    # even if one member never saw a given class.
    labels = sorted({k for d in dists for k in d})
    if not labels:
        return None
    matrix = [[float(d.get(label, 0.0)) for label in labels] for d in dists]

    spatial, temporal = _member_distances(cluster)
    posterior = integrate_cluster(
        class_labels=labels,
        member_distributions=matrix,
        spatial_distances_m=spatial,
        temporal_distances_s=temporal,
        sigma_floor_m=settings.cluster_rbf_sigma_floor_m,
        sigma_floor_s=settings.cluster_rbf_sigma_floor_seconds,
        prior_concentration=settings.cluster_prior_concentration,
    )
    # 'pothole' is the class the read path, the dashboard and the repair workflow all
    # key off, so it is what `confidence` must carry.
    confidence = posterior.distribution.get(CLASS_POTHOLE, 0.0)
    return confidence, json.dumps(posterior.distribution), posterior.weights


async def _compute_clusters(conn, *, window, min_conf, eps_m, min_points):
    """Run the member gate + DBSCAN. Returns (rows, n_members, mean_lat, eps_units).

    Separated from the write phase so a test can interpose between the two and
    reproduce the repair-mid-run race deterministically.
    """
    stats = await conn.fetchrow(
        _MEMBER_STATS_SQL,
        window,
        min_conf,
        eps_m,
        settings.fusion_frame_only_enabled,
        settings.fusion_frame_only_min_probability,
    )
    n_members = stats["n"] or 0
    if n_members < min_points:
        return None, n_members, None, None

    # Web-Mercator distorts distance by 1/cos(lat); scale eps so the planar
    # threshold corresponds to eps_m ground meters at this latitude.
    mean_lat = float(stats["mean_lat"])
    eps_units = eps_m / max(math.cos(math.radians(mean_lat)), 1e-6)

    clusters = await conn.fetch(
        _CLUSTER_SQL,
        window,
        min_conf,
        eps_m,
        settings.fusion_frame_only_enabled,
        settings.fusion_frame_only_min_probability,
        eps_units,
        min_points,
    )
    return clusters, n_members, mean_lat, eps_units


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

            clusters, n_members, mean_lat, eps_units = await _compute_clusters(
                conn, window=window, min_conf=min_conf, eps_m=eps_m, min_points=min_points
            )
            if clusters is None:
                logger.info("Cluster gate not met: %d members (< %d).", n_members, min_points)
                return 0

            # Separate carriageways before anything else looks at these groups: the
            # centroid, the mean bearing and the integrated distribution are all wrong
            # for a group that spans two directions.
            if settings.cluster_bearing_aware:
                clusters = _direction_split_clusters(
                    clusters, settings.cluster_bearing_tolerance_deg
                )

            run_id = uuid4().hex
            matched: list[str] = []
            skipped = 0
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

                legacy_fallbacks = 0
                for c in clusters:
                    lon, lat = float(c["centroid_lon"]), float(c["centroid_lat"])

                    # Spatiotemporal integration (Phase 2.2c). Falls back to the
                    # legacy member mean when the members predate
                    # sensor_class_probs, so this is safe to enable on a database
                    # of un-rescored observations.
                    confidence = c["confidence"]
                    class_probs = None
                    if settings.cluster_spatiotemporal_enabled:
                        integrated = _integrate_cluster_row(c)
                        if integrated is not None:
                            confidence, class_probs, _ = integrated
                        else:
                            legacy_fallbacks += 1

                    match_bearing = (
                        c["bearing_deg"] if settings.cluster_bearing_aware else None
                    )
                    existing = await conn.fetchval(
                        _FIND_EXISTING_SQL, lon, lat, eps_m, matched,
                        match_bearing, settings.cluster_bearing_tolerance_deg,
                    )
                    if existing is not None:
                        cluster_id = existing
                        await conn.execute(
                            _UPDATE_CLUSTER_SQL, cluster_id, lon, lat,
                            c["severity"], confidence, c["observation_count"],
                            c["distinct_devices"], c["last_seen"],
                            c["bearing_deg"], class_probs,
                        )
                    else:
                        # Re-check for a repair that landed while this run was
                        # computing; without it the members an operator just closed
                        # out would come straight back as a new, un-repaired cluster.
                        covered_by = await conn.fetchval(
                            _FIND_REPAIRED_COVERING_SQL, lon, lat, eps_m, c["last_seen"]
                        )
                        if covered_by is not None:
                            logger.info(
                                "Skipping insert at (%.6f, %.6f): covered by repaired "
                                "cluster %s.", lon, lat, covered_by,
                            )
                            skipped += 1
                            continue
                        cluster_id = "clu_" + uuid4().hex
                        await conn.execute(
                            _INSERT_CLUSTER_SQL, cluster_id, lon, lat,
                            c["severity"], confidence, c["observation_count"],
                            c["distinct_devices"], c["last_seen"],
                            c["bearing_deg"], class_probs,
                        )
                    matched.append(cluster_id)

                    await conn.execute(_DELETE_LINKS_SQL, cluster_id)
                    for mid, mc, mk in zip(
                        c["member_ids"], c["member_confidences"], c["member_kinds"]
                    ):
                        await conn.execute(_INSERT_LINK_SQL, cluster_id, mid, mc, mk)

                written = len(clusters) - skipped
                await conn.execute(_UPDATE_CLUSTER_RUN_SQL, run_id, written)
            logger.info(
                "Cluster run %s: %d members → %d clusters (%d skipped as repaired, "
                "%d on the legacy mean for want of a class posterior).",
                run_id, n_members, written, skipped, legacy_fallbacks,
            )
            return written
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _CLUSTER_LOCK_KEY)
