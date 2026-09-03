"""Read side of the operator dashboard's cluster detail panel (Phase 2.5).

Four queries on one acquired connection rather than one joined statement. The
reason is correctness, not style: _PAIRING_SQL (app/fusion/service.py) picks the
best observation *per frame*, so several frames can pair to one observation and
one observation fans out to several frames. A single join multiplies member rows
by their frame count, and LIMIT then truncates the multiplied set at an arbitrary
boundary — "200 rows" that represent 40 observations. The panel issues one HTTP
request either way, which is what the p95 budget measures; four pooled fetches
cost about a millisecond of it.

The cluster -> frames path is the part that is easy to get wrong:

    observation_cluster_link (kind='observation')
      -> asset_observation.client_id
      -> fusion_pair.event_client_id
      -> fusion_pair.frame_client_id
      -> asset_frame.client_id

The clustering job only ever writes kind='observation' links, so there are no
'frame' rows despite the CHECK constraint permitting them. And
asset_frame.event_client_id is a nullable, unindexed, no-FK client-supplied hint
that is almost always NULL — fusion reconstructs the pairing by device, time and
distance and never reads it. Joining on either yields a permanently empty panel.

Every join step is an index scan on an existing primary key, so this module adds
no indexes: observation_cluster_link's PK leads with cluster_id, fusion_pair's
leads with event_client_id, and asset_observation / asset_frame are keyed on
client_id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg

from app.models.clusters import (
    ClusterDetailResponse,
    ClusterFrameItem,
    ClusterMemberItem,
    FrameDetailResponse,
    RepairLogItem,
)
from app.services.detection_boxes import parse_detection_boxes, parse_vlm_verdict

logger = logging.getLogger(__name__)

# Bounds on the panel payload. observation_count can run to thousands, and with
# thumbnails deliberately not built the images — not the SQL — are the latency:
# each frame is a separate authenticated request through the browser's
# 6-connection ceiling.
MAX_MEMBERS = 200
MAX_FRAMES = 12
MAX_REPAIR_HISTORY = 20

_HEADER_SQL = """
SELECT
    c.cluster_id, c.asset_type,
    ST_Y(c.centroid::geometry) AS lat,
    ST_X(c.centroid::geometry) AS lon,
    c.severity, c.confidence, c.observation_count, c.distinct_devices,
    -- Both written by the clustering job (migrations/015). The panel has always
    -- had a row for distinct_passes and this query never selected it, so the
    -- console printed "0 passes" about every cluster while the table said 1.
    c.distinct_passes, c.member_span_s,
    c.last_seen, c.source, c.repaired_at, c.created_at, c.updated_at
FROM asset_cluster c
WHERE c.cluster_id = $1
"""

# device_ref is a per-cluster ordinal, so device_id never leaves Postgres. Window
# functions are evaluated before LIMIT, so the ranking covers the whole member
# set and stays consistent with the cluster's distinct_devices even when the
# returned list is truncated.
_MEMBERS_SQL = """
SELECT
    o.client_id,
    ST_Y(o.geom::geometry) AS lat,
    ST_X(o.geom::geometry) AS lon,
    o.ts_utc,
    o.speed_mps,
    o.accuracy_m,
    o.sensor_class,
    o.sensor_p_pothole,
    o.sensor_severity,
    o.sensor_is_outlier,
    l.fused_confidence,
    dense_rank() OVER (ORDER BY o.device_id) AS device_rank
FROM observation_cluster_link l
JOIN asset_observation o ON o.client_id = l.member_id
WHERE l.cluster_id = $1
  AND l.kind = 'observation'
ORDER BY o.ts_utc DESC, o.client_id
LIMIT $2
"""

# DISTINCT ON collapses the fan-out to one row per frame carrying its strongest
# pairing; the outer query then re-sorts by usefulness before LIMIT, so the cap
# keeps the best frames rather than whichever ones DISTINCT ON happened to order
# first. jpeg_url and device_id are deliberately not selected — the stored path
# embeds device_id.
_FRAMES_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (f.client_id)
        f.client_id,
        ST_Y(f.geom::geometry) AS lat,
        ST_X(f.geom::geometry) AS lon,
        f.ts_utc,
        f.device_probability,
        f.server_probability,
        f.server_model_id,
        f.detected_at,
        f.server_detections,
        f.device_detections,
        fp.event_client_id AS paired_observation_id,
        fp.fused_confidence,
        fp.delta_ms,
        fp.delta_m
    FROM observation_cluster_link l
    JOIN fusion_pair fp ON fp.event_client_id = l.member_id
    JOIN asset_frame  f  ON f.client_id       = fp.frame_client_id
    WHERE l.cluster_id = $1
      AND l.kind = 'observation'
    ORDER BY f.client_id, fp.fused_confidence DESC NULLS LAST, fp.event_client_id
) d
ORDER BY d.fused_confidence DESC NULLS LAST, d.ts_utc DESC, d.client_id
LIMIT $2
"""

# LEFT JOIN so an entry survives deletion of the account that wrote it — the
# reason repair_log.user_id has no foreign key.
_HISTORY_SQL = """
SELECT r.repair_id, r.action, r.note, r.user_id, r.created_at, u.email AS user_email
FROM repair_log r
LEFT JOIN staff_user u ON u.user_id = r.user_id
WHERE r.cluster_id = $1
ORDER BY r.created_at DESC, r.repair_id DESC
LIMIT $2
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _device_label(rank: int) -> str:
    """Render a 1-based device ordinal as A, B, … Z, AA, AB, …"""
    label = ""
    n = rank
    while n > 0:
        n, rem = divmod(n - 1, 26)
        label = chr(ord("A") + rem) + label
    return label or "A"


async def get_cluster_detail(
    pool: asyncpg.Pool, *, cluster_id: str, frame_limit: int = MAX_FRAMES
) -> ClusterDetailResponse | None:
    """Return the full detail payload for one cluster, or None if it doesn't exist.

    Members and frames are fetched with LIMIT n+1 so the response can tell the UI
    that it is looking at a truncated list rather than the whole cluster.
    """
    frame_limit = max(1, min(frame_limit, MAX_FRAMES))

    async with pool.acquire() as conn:
        header = await conn.fetchrow(_HEADER_SQL, cluster_id)
        if header is None:
            return None

        member_rows = await conn.fetch(_MEMBERS_SQL, cluster_id, MAX_MEMBERS + 1)
        frame_rows = await conn.fetch(_FRAMES_SQL, cluster_id, frame_limit + 1)
        history_rows = await conn.fetch(_HISTORY_SQL, cluster_id, MAX_REPAIR_HISTORY)

    members_truncated = len(member_rows) > MAX_MEMBERS
    frames_truncated = len(frame_rows) > frame_limit

    members = [
        ClusterMemberItem(
            client_id=r["client_id"],
            lat=r["lat"],
            lon=r["lon"],
            ts=_iso(r["ts_utc"]),
            device_ref=_device_label(r["device_rank"]),
            speed_mps=r["speed_mps"],
            accuracy_m=r["accuracy_m"],
            sensor_class=r["sensor_class"],
            sensor_p_pothole=r["sensor_p_pothole"],
            sensor_severity=r["sensor_severity"],
            sensor_is_outlier=r["sensor_is_outlier"],
            fused_confidence=r["fused_confidence"],
        )
        for r in member_rows[:MAX_MEMBERS]
    ]

    frames = [
        ClusterFrameItem(
            client_id=r["client_id"],
            lat=r["lat"],
            lon=r["lon"],
            ts=_iso(r["ts_utc"]),
            image_url=f"/api/v1/frames/{r['client_id']}/image",
            device_probability=r["device_probability"],
            server_probability=r["server_probability"],
            server_model_id=r["server_model_id"],
            detected_at=_iso(r["detected_at"]),
            paired_observation_id=r["paired_observation_id"],
            fused_confidence=r["fused_confidence"],
            delta_ms=r["delta_ms"],
            delta_m=r["delta_m"],
            # Parsed rather than passed through: the detections column also carries the
            # hybrid backend's {"_vlm_verdict": ...} element, which has no bbox and would
            # otherwise reach a box renderer. See app/services/detection_boxes.py.
            server_boxes=parse_detection_boxes(r["server_detections"]),
            device_boxes=parse_detection_boxes(r["device_detections"]),
            vlm_verdict=parse_vlm_verdict(r["server_detections"]),
        )
        for r in frame_rows[:frame_limit]
    ]

    history = [
        RepairLogItem(
            repair_id=r["repair_id"],
            action=r["action"],
            note=r["note"],
            user_id=r["user_id"],
            user_email=r["user_email"],
            at=_iso(r["created_at"]),
        )
        for r in history_rows
    ]

    return ClusterDetailResponse(
        cluster_id=header["cluster_id"],
        asset_type=header["asset_type"],
        lat=header["lat"],
        lon=header["lon"],
        severity=header["severity"],
        confidence=header["confidence"],
        observation_count=header["observation_count"],
        distinct_devices=header["distinct_devices"],
        # distinct_passes is INT NOT NULL DEFAULT 0 (015), so it needs no coalesce.
        # member_span_s is nullable -- a single-member cluster has no span.
        distinct_passes=header["distinct_passes"],
        member_span_s=header["member_span_s"],
        last_seen=_iso(header["last_seen"]),
        source=header["source"],
        repaired_at=_iso(header["repaired_at"]),
        created_at=_iso(header["created_at"]),
        updated_at=_iso(header["updated_at"]),
        members=members,
        members_truncated=members_truncated,
        frames=frames,
        frames_truncated=frames_truncated,
        repair_history=history,
        generated_at=datetime.now(UTC).isoformat(),
    )


_FRAME_STORAGE_SQL = "SELECT jpeg_url FROM asset_frame WHERE client_id = $1"


async def get_frame_storage_url(pool: asyncpg.Pool, *, client_id: str) -> str | None:
    """Return a frame's stored jpeg_url, or None if there is no such frame.

    The image route looks the path up here rather than accepting one from the
    client; the value is still passed through the containment check in
    frame_service before anything is read.
    """
    async with pool.acquire() as conn:
        return await conn.fetchval(_FRAME_STORAGE_SQL, client_id)


# ── One frame, on its own ─────────────────────────────────────────────────────
#
# Same anonymity rule as _FRAMES_SQL: jpeg_url and device_id are deliberately not
# selected, because the stored path embeds device_id.
#
# LEFT JOIN, not JOIN. The map's frames layer includes frames that never paired --
# that is what `frameStatus`'s "scored but unpaired" case reports, and 98.6% of
# pothole-classed observations have no coincident frame at all. An inner join here
# would 404 exactly the frames an operator is most likely to be curious about.
#
# DISTINCT ON because a frame can pair with more than one observation; the primary
# pair wins, matching _FRAMES_SQL's ordering so the two surfaces agree about which
# pairing a frame "has".
_FRAME_DETAIL_SQL = """
SELECT DISTINCT ON (f.client_id)
    f.client_id,
    ST_Y(f.geom::geometry) AS lat,
    ST_X(f.geom::geometry) AS lon,
    f.ts_utc,
    f.device_probability,
    f.server_probability,
    f.server_model_id,
    f.detected_at,
    f.server_detections,
    f.device_detections,
    fp.event_client_id AS paired_observation_id,
    fp.fused_confidence,
    fp.delta_ms,
    fp.delta_m
FROM asset_frame f
LEFT JOIN fusion_pair fp ON fp.frame_client_id = f.client_id
WHERE f.client_id = $1
ORDER BY f.client_id, fp.is_primary DESC NULLS LAST, fp.fused_confidence DESC NULLS LAST
"""


async def get_frame_detail(pool: asyncpg.Pool, client_id: str) -> FrameDetailResponse | None:
    """One frame's full evidence, or None if there is no such frame.

    Serves the map: the frames tile carries `server_box_count` but not the boxes, because
    frame-relative geometry is meaningless in map space, so the viewer needs a round trip
    to draw anything.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FRAME_DETAIL_SQL, client_id)
    if row is None:
        return None

    return FrameDetailResponse(
        client_id=row["client_id"],
        lat=row["lat"],
        lon=row["lon"],
        ts=_iso(row["ts_utc"]),
        image_url=f"/api/v1/frames/{row['client_id']}/image",
        device_probability=row["device_probability"],
        server_probability=row["server_probability"],
        server_model_id=row["server_model_id"],
        detected_at=_iso(row["detected_at"]),
        paired_observation_id=row["paired_observation_id"],
        fused_confidence=row["fused_confidence"],
        delta_ms=row["delta_ms"],
        delta_m=row["delta_m"],
        # Parsed, not passed through: server_detections also carries the hybrid backend's
        # {"_vlm_verdict": ...} element, which has no bbox and would reach a box renderer.
        server_boxes=parse_detection_boxes(row["server_detections"]),
        device_boxes=parse_detection_boxes(row["device_detections"]),
        vlm_verdict=parse_vlm_verdict(row["server_detections"]),
    )
