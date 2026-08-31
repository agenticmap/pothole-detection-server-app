"""Test configuration and shared fixtures."""

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

# Set test environment variables BEFORE importing app modules.
# DATABASE_URL uses setdefault so a different DB can be targeted by exporting
# DATABASE_URL before running pytest. The default matches this project's
# docker-compose, which maps the PostGIS container to host port 5433.
#
# Note the database is `pothole_test`, NOT the dev `pothole_db`: the db_pool
# fixture TRUNCATEs every table, and pointing it at the dev database destroys
# collected drive data. conftest.py enforces this with an allow-list.
os.environ.setdefault(
    "DATABASE_URL", "postgresql://pothole:pothole@localhost:5433/pothole_test"
)
os.environ["ENV"] = "development"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_LOCAL_PATH"] = "./test_storage"
os.environ["RATE_LIMIT_EVENTS_PER_HOUR"] = "1000"
os.environ["RATE_LIMIT_FRAMES_PER_HOUR"] = "1000"
# Keep the in-process fit/fusion scheduler off so HTTP tests run the lifespan
# without firing background jobs. DB fusion tests call the jobs directly.
os.environ["FUSION_ENABLED"] = "false"
os.environ["SENSOR_FIT_ENABLED"] = "false"
# Pin the clustering window to the shipped default.
#
# Settings reads the developer's .env, so without this the suite inherits it —
# and several tests assert on the window by construction (inserting a 90-day-old
# cluster and expecting it excluded). A dev machine that sets
# CLUSTER_WINDOW_DAYS=3650 to stop an archive database ageing out then makes
# those tests fail, which is the harmless direction. The dangerous direction is
# the same mechanism silently making an assertion vacuous, so this is pinned
# rather than left to chance — the same reason the four settings above are.
os.environ["CLUSTER_WINDOW_DAYS"] = "30"

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Create an async test client for the FastAPI app.

    Runs the app lifespan so the asyncpg pool is initialized (httpx's
    ASGITransport does not trigger lifespan on its own). The fit/fusion
    scheduler stays off via FUSION_ENABLED/SENSOR_FIT_ENABLED in the env above.
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac


@pytest.fixture(autouse=True)
def clear_rate_limits():
    """Kept as a fixture name only — the reset itself moved.

    Rate-limit counters used to be module-level dicts, so clearing them was a
    synchronous call. They are now rows in `device_rate_limit`, and the db_pool
    fixture's TRUNCATE owns them along with every other table.

    This stays as an autouse no-op rather than being deleted because tests import
    it by name, and because the alternative — opening a connection per test to
    truncate one table — costs a round trip on all ~470 of them to protect
    against an overrun that the 1000/hour test limit makes unreachable.
    """
    yield


def make_valid_event(client_id: str = "test-event-001") -> dict:
    """Create a minimal valid event payload for testing."""
    return {
        "client_id": client_id,
        "schema_version": 1,
        "ts": "2026-05-27T10:30:00Z",
        "lat": 43.6532,
        "lon": -79.3832,
        "speed_mps": 12.5,
        "bearing_deg": 180.0,
        "speed_accuracy_mps": 1.2,
        "accel_max_g": 2.3,
        "accel_std": 0.8,
        "magnitude": 3.1,
        "confidence": 0.95,
    }


def make_valid_frame_metadata(client_id: str = "test-frame-001") -> dict:
    """Create valid frame metadata for testing."""
    return {
        "client_id": client_id,
        "ts": "2026-05-27T10:30:00Z",
        "lat": 43.6532,
        "lon": -79.3832,
        "device_p_on_device": 0.85,
        "model_id": "road_gate_stub_v1",
    }


# Minimal valid JPEG (smallest possible JPEG — 1x1 pixel white)
MINIMAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000"
    "ffdb004300080606070605080707070909080a0c"
    "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c"
    "20242e2720222c231c1c2837292c30313434341f"
    "27393d38323c2e333432ffc0000b080001000101"
    "011100ffc4001f000001050101010101010000000"
    "0000000000102030405060708090a0bffc4002610"
    "0002010303020403050504040000017d010203000"
    "411053121410613516107227114328191a1082342"
    "b1c11552d1f02433627282090a161718191a2526"
    "2728292a3435363738393a434445464748494a535"
    "45556575859ffd9"
)
