"""Shared test fixtures — re-exported from tests/__init__.py for clarity."""

import os
from datetime import datetime
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from app.config import settings
from app.database import create_pool, run_migrations
from tests import (  # noqa: F401
    MINIMAL_JPEG,
    clear_rate_limits,
    client,
    make_valid_event,
    make_valid_frame_metadata,
)

# This fixture TRUNCATEs every table, so it must never point at a working database.
# Guard on the database name rather than trusting whoever set DATABASE_URL: an
# accidental `pytest` against the dev DB silently destroys collected drive data,
# which is unrecoverable. See docker-compose.yml's `pothole_test` database.
_ALLOWED_TEST_DATABASES = frozenset({"pothole_test", "pothole_ci"})


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _in_ci() -> bool:
    """True when running on a CI runner.

    Locally, an unreachable Postgres skips the DB-backed tests so the pure-unit
    suite still runs without `docker compose up`. On CI that leniency is a trap:
    a service container that never came up would report a green run with most of
    the suite silently skipped, which is the exact shape of failure CI exists to
    catch. There, an unreachable database is an error, not a reason to say nothing.

    `CI` is set by GitHub Actions, GitLab, CircleCI and Travis alike.
    """
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


@pytest_asyncio.fixture
async def db_pool():
    """A real asyncpg pool with migrations applied.

    Skips the test if a local Postgres isn't reachable (so unit tests still run
    in environments without a database) -- except on CI, where an unreachable
    database is a failure. See _in_ci().
    """
    db_name = _database_name(settings.database_url)
    if db_name not in _ALLOWED_TEST_DATABASES:
        pytest.fail(
            f"Refusing to run destructive tests against database {db_name!r}. "
            f"These fixtures TRUNCATE every table. Point DATABASE_URL at one of "
            f"{sorted(_ALLOWED_TEST_DATABASES)}, e.g.\n"
            f"  DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_test pytest"
        )
    try:
        pool = await create_pool()
    except Exception as e:  # noqa: BLE001 — any connection failure → skip, or fail on CI
        message = (
            f"Postgres not available: {e}\n"
            f"If {db_name!r} does not exist yet, create it once with:\n"
            f"  docker compose exec postgres createdb -U pothole {db_name}"
        )
        if _in_ci():
            pytest.fail(message)
        pytest.skip(message)
    await run_migrations(pool)
    # Clean slate for fusion/sensor tables so tests are independent.
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE device_rate_limit, repair_log, refresh_token, org_member, staff_user, org, "
            "observation_cluster_link, asset_cluster, cluster_run, "
            "model_disagreement, frame_box, frame_label_history, frame_label, fusion_pair, "
            "fusion_run, sensor_model, "
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
