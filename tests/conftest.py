"""Shared test fixtures — re-exported from tests/__init__.py for clarity."""

from datetime import datetime

import pytest
import pytest_asyncio

from app.database import create_pool, run_migrations
from tests import (  # noqa: F401
    MINIMAL_JPEG,
    clear_rate_limits,
    client,
    make_valid_event,
    make_valid_frame_metadata,
)


@pytest_asyncio.fixture
async def db_pool():
    """A real asyncpg pool with migrations applied.

    Skips the test if a local Postgres isn't reachable (so unit tests still run
    in environments without a database).
    """
    try:
        pool = await create_pool()
    except Exception as e:  # noqa: BLE001 — any connection failure → skip
        pytest.skip(f"Postgres not available: {e}")
    await run_migrations(pool)
    # Clean slate for fusion/sensor tables so tests are independent.
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE observation_cluster_link, asset_cluster, cluster_run, "
            "fusion_pair, fusion_run, sensor_model, "
            "asset_frame, asset_observation RESTART IDENTITY CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


_INSERT_OBS_SQL = """
INSERT INTO asset_observation (
    client_id, device_id, asset_type, schema_version, ts_utc, geom,
    accel_max_g, accel_std, magnitude, gbar_in_max, speed_mps, bearing_deg, confidence
)
VALUES (
    $1, $2, 'pothole', 1, $3::timestamptz,
    ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
    $6, $7, $8, $9, $10, $11, 1.0
)
ON CONFLICT (client_id) DO NOTHING
"""

_INSERT_FRAME_SQL = """
INSERT INTO asset_frame (
    client_id, device_id, ts_utc, geom, device_probability, jpeg_url
)
VALUES (
    $1, $2, $3::timestamptz,
    ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
    $6, $7
)
ON CONFLICT (client_id) DO NOTHING
"""


async def insert_observation(
    conn,
    client_id: str,
    *,
    device_id: str = "dev-1",
    ts: str = "2026-05-27T10:30:00+00:00",
    lat: float = 43.6532,
    lon: float = -79.3832,
    accel_max_g: float = 2.5,
    accel_std: float = 1.0,
    magnitude: float = 3.0,
    gbar_in_max: float = 2.0,
    speed_mps: float = 12.0,
    bearing_deg: float = 180.0,
) -> None:
    """Insert a sensor observation row directly (bypassing the HTTP layer)."""
    await conn.execute(
        _INSERT_OBS_SQL, client_id, device_id, datetime.fromisoformat(ts), lon, lat,
        accel_max_g, accel_std, magnitude, gbar_in_max, speed_mps, bearing_deg,
    )


async def insert_frame(
    conn,
    client_id: str,
    *,
    device_id: str = "dev-1",
    ts: str = "2026-05-27T10:30:00+00:00",
    lat: float = 43.6532,
    lon: float = -79.3832,
    device_probability: float = 0.8,
    jpeg_url: str = "dev-1/frame.jpg",
) -> None:
    """Insert a camera frame row directly (bypassing the HTTP layer)."""
    await conn.execute(
        _INSERT_FRAME_SQL, client_id, device_id, datetime.fromisoformat(ts),
        lon, lat, device_probability, jpeg_url,
    )
