"""Integration tests for the fit + fusion jobs (require a local Postgres+PostGIS).

Skipped automatically by the db_pool fixture when no database is reachable.
"""

import numpy as np
import pytest

from app.fusion.service import run_fit_job, run_fusion_job
from app.sensor_model.store import load_active_model
from tests.conftest import insert_frame, insert_observation

pytestmark = pytest.mark.asyncio


async def _seed_three_blobs(conn, n_per_blob: int, prefix: str):
    """Insert 3*n_per_blob observations forming 3 separated [ratio, gbar] blobs."""
    rng = np.random.default_rng(0)
    i = 0
    for rc, gc in [(1.0, 1.0), (4.0, 4.0), (8.0, 8.0)]:
        for _ in range(n_per_blob):
            ratio = max(0.1, float(rng.normal(rc, 0.3)))
            gbar = max(0.1, float(rng.normal(gc, 0.3)))
            await insert_observation(
                conn, f"{prefix}-{i}", device_id="dev-1",
                magnitude=ratio, accel_std=1.0, gbar_in_max=gbar, speed_mps=12.0,
            )
            i += 1


async def test_fit_job_gate_and_activation(db_pool):
    # Below the gate (default N_min=200) -> no model.
    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=40, prefix="a")  # 120 rows < 200
    assert await run_fit_job(db_pool) is None
    assert await load_active_model(db_pool) is None

    # Add fresh rows to cross the gate -> a model is fit and activated.
    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=50, prefix="b")  # +150 -> 270 >= 200
    version = await run_fit_job(db_pool)
    assert version is not None
    model = await load_active_model(db_pool)
    assert model is not None
    assert model.k == 3
    assert model.iforest is not None


async def test_fusion_pairs_nearest_observation(db_pool):
    async with db_pool.acquire() as conn:
        # One frame, two candidate observations: one inside the window, one outside.
        await insert_frame(
            conn, "frame-1", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00", lat=43.6532, lon=-79.3832,
            device_probability=0.8,
        )
        # In-window: same spot, ~0.5s earlier.
        await insert_observation(
            conn, "obs-near", device_id="dev-1",
            ts="2026-05-27T10:29:59.5+00:00", lat=43.6532, lon=-79.3832,
            magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0,
        )
        # Out-of-window: ~10s away in time.
        await insert_observation(
            conn, "obs-far", device_id="dev-1",
            ts="2026-05-27T10:30:10+00:00", lat=43.6532, lon=-79.3832,
            magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0,
        )

    # No active model -> heuristic fallback engine, but pairing still happens.
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='frame-1'"
        )
        run = await conn.fetchrow("SELECT inputs_count, outputs_count FROM fusion_run")

    assert len(pairs) == 1
    assert pairs[0]["event_client_id"] == "obs-near"
    assert pairs[0]["frame_client_id"] == "frame-1"
    assert 0.0 <= pairs[0]["fused_confidence"] <= 1.0
    assert abs(pairs[0]["delta_ms"]) < 3000
    assert frame["processed_at"] is not None
    assert run["inputs_count"] == 1 and run["outputs_count"] == 1


async def test_frame_with_no_candidate_is_marked_processed(db_pool):
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "lonely-frame", device_id="dev-1")
        # An observation on a DIFFERENT device must not pair.
        await insert_observation(
            conn, "other-dev-obs", device_id="dev-2",
            ts="2026-05-27T10:30:00+00:00", magnitude=6.0, accel_std=1.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='lonely-frame'"
        )
    assert len(pairs) == 0
    assert frame["processed_at"] is not None  # still marked, won't be rescanned


async def test_fusion_is_idempotent_and_deterministic(db_pool):
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f1", device_id="dev-1", device_probability=0.7)
        await insert_observation(
            conn, "o1", device_id="dev-1",
            ts="2026-05-27T10:29:59.7+00:00", magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='f1'"
        )
        # Reset the frame so it re-pairs; confidence must be byte-identical.
        await conn.execute("UPDATE asset_frame SET processed_at = NULL WHERE client_id='f1'")
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='f1'"
        )
    assert len(pairs) == 1  # upsert, not duplicate
    assert pairs[0]["fused_confidence"] == first["fused_confidence"]
