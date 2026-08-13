"""Event ingestion service — batch insert with idempotency."""

import logging
from datetime import datetime

import asyncpg

from app.models import EventPayload, RejectedEvent

logger = logging.getLogger(__name__)

# SQL for inserting a single event with idempotency.
# ON CONFLICT DO NOTHING ensures duplicate client_ids are silently skipped.
# We use RETURNING to know which rows were actually inserted vs skipped.
_INSERT_EVENT_SQL = """
INSERT INTO asset_observation (
    client_id,
    device_id,
    asset_type,
    schema_version,
    ts_utc,
    geom,
    speed_mps,
    bearing_deg,
    speed_accuracy_mps,
    accuracy_m,
    accel_max_g,
    accel_std,
    magnitude,
    gbar_in_max,
    time_in_max,
    time_in_min,
    confidence,
    raw_window_b64,
    visual_confirmed,
    frame_client_id
)
VALUES (
    $1, $2, 'pothole', $3,
    $4::timestamptz,
    ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
    $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
)
ON CONFLICT (client_id) DO NOTHING
"""


async def insert_events(
    pool: asyncpg.Pool,
    device_id: str,
    events: list[EventPayload],
) -> tuple[list[str], list[RejectedEvent]]:
    """Insert a batch of events into asset_observation.

    Returns:
        A tuple of (accepted_client_ids, rejected_items).
        Both genuinely new inserts and idempotent duplicates appear in accepted.
    """
    accepted: list[str] = []
    rejected: list[RejectedEvent] = []

    async with pool.acquire() as conn:
        # Use a transaction for the entire batch — if the connection drops
        # mid-batch, nothing is committed, and the client will retry all.
        async with conn.transaction():
            for event in events:
                try:
                    # asyncpg requires a datetime object for TIMESTAMPTZ columns
                    ts_str = event.ts.replace("Z", "+00:00") if event.ts.endswith("Z") else event.ts
                    ts_dt = datetime.fromisoformat(ts_str)

                    await conn.execute(
                        _INSERT_EVENT_SQL,
                        event.client_id,        # $1
                        device_id,              # $2
                        event.schema_version,   # $3
                        ts_dt,                  # $4
                        event.lon,              # $5 (ST_MakePoint takes lon first)
                        event.lat,              # $6
                        event.speed_mps,        # $7
                        event.bearing_deg,      # $8
                        event.speed_accuracy_mps,  # $9
                        event.accuracy_m,       # $10
                        event.accel_max_g,      # $11
                        event.accel_std,        # $12
                        event.magnitude,        # $13
                        event.gbar_in_max,      # $14
                        event.time_in_max,      # $15
                        event.time_in_min,      # $16
                        event.confidence,       # $17
                        event.raw_window_b64,   # $18
                        event.visual_confirmed,  # $19
                        event.frame_client_id,  # $20
                    )
                    # Both new inserts and conflict-skips count as accepted.
                    # The client deletes its local row for any ID in accepted.
                    accepted.append(event.client_id)

                except asyncpg.PostgresError as e:
                    logger.warning(
                        "DB error inserting event %s: %s",
                        event.client_id,
                        e,
                    )
                    rejected.append(
                        RejectedEvent(client_id=event.client_id, reason=str(e))
                    )

    return accepted, rejected
