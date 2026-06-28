"""Integration tests for the clustering job (require a local Postgres+PostGIS).

Skipped automatically by the db_pool fixture when no database is reachable.
"""

import pytest

from app.fusion.service import run_cluster_job
from tests.conftest import insert_frame, insert_observation

pytestmark = pytest.mark.asyncio

# A point and small offsets around it. 1° lat ≈ 111 km, so ±0.00006° ≈ ±7 m
# (well inside the 25 m eps); +0.0020° ≈ 222 m (a separate location).
BASE_LAT, BASE_LON = 43.6532, -79.3832


async def _insert_pothole(
    conn, client_id, *, device_id="dev-1", ts="2026-05-27T10:30:00+00:00",
    lat=BASE_LAT, lon=BASE_LON, p=0.9, severity=0.5,
):
    """Insert an observation already scored as a (non-outlier) pothole."""
    await insert_observation(conn, client_id, device_id=device_id, ts=ts, lat=lat, lon=lon)
    await conn.execute(
        "UPDATE asset_observation SET sensor_class='pothole', sensor_p_pothole=$2, "
        "sensor_severity=$3, sensor_is_outlier=FALSE, scored_at=now() WHERE client_id=$1",
        client_id, p, severity,
    )


async def test_forms_a_cluster(db_pool):
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "p1", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "p2", device_id="dev-2", lat=BASE_LAT + 0.00003)
        await _insert_pothole(conn, "p3", device_id="dev-2", lat=BASE_LAT - 0.00003)

    n = await run_cluster_job(db_pool)
    assert n == 1

    async with db_pool.acquire() as conn:
        clusters = await conn.fetch("SELECT * FROM asset_cluster")
        links = await conn.fetch("SELECT * FROM observation_cluster_link")
        dist = await conn.fetchval(
            "SELECT ST_Distance(centroid, ST_SetSRID(ST_MakePoint($1,$2),4326)::geography) "
            "FROM asset_cluster LIMIT 1",
            BASE_LON, BASE_LAT,
        )

    assert len(clusters) == 1
    c = clusters[0]
    assert c["observation_count"] == 3
    assert c["distinct_devices"] == 2
    assert c["source"] == "crowd"
    assert c["repaired_at"] is None
    assert dist < 30.0  # centroid within a few meters of the true point
    assert {ln["member_id"] for ln in links} == {"p1", "p2", "p3"}
    assert all(ln["kind"] == "observation" for ln in links)


async def test_noise_below_minpoints_is_rejected(db_pool):
    async with db_pool.acquire() as conn:
        # Two single-device observations >100 m apart: neither reaches min_points.
        await _insert_pothole(conn, "lone-a", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "lone-b", device_id="dev-1", lat=BASE_LAT + 0.0020)

    n = await run_cluster_job(db_pool)
    assert n == 0
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM asset_cluster") == 0


async def test_clustering_is_idempotent(db_pool):
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "p1", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "p2", device_id="dev-2", lat=BASE_LAT + 0.00003)
        await _insert_pothole(conn, "p3", device_id="dev-2", lat=BASE_LAT - 0.00003)

    await run_cluster_job(db_pool)
    async with db_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT cluster_id, created_at, updated_at FROM asset_cluster"
        )

    await run_cluster_job(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT cluster_id, created_at, updated_at FROM asset_cluster")
        links = await conn.fetchval("SELECT count(*) FROM observation_cluster_link")

    assert len(rows) == 1  # matched in place, not duplicated
    assert rows[0]["cluster_id"] == first["cluster_id"]
    assert rows[0]["created_at"] == first["created_at"]  # creation preserved
    assert rows[0]["updated_at"] >= first["updated_at"]
    assert links == 3  # links rebuilt, not doubled


async def test_repaired_cluster_is_preserved_and_new_defect_forms_new_cluster(db_pool):
    # Form an initial cluster from "old" detections.
    async with db_pool.acquire() as conn:
        for i, dev in enumerate(["dev-1", "dev-2", "dev-2"]):
            await _insert_pothole(
                conn, f"old-{i}", device_id=dev, ts="2026-06-01T10:00:00+00:00",
                lat=BASE_LAT + (i - 1) * 0.00003,
            )
    await run_cluster_job(db_pool)
    async with db_pool.acquire() as conn:
        original = await conn.fetchval("SELECT cluster_id FROM asset_cluster")
        # Admin marks it repaired (after the old detections' timestamps).
        await conn.execute(
            "UPDATE asset_cluster SET repaired_at='2026-06-10T00:00:00+00:00' "
            "WHERE cluster_id=$1",
            original,
        )
        # The defect returns: fresh detections in the same spot, newer than the repair.
        for i, dev in enumerate(["dev-1", "dev-2", "dev-2"]):
            await _insert_pothole(
                conn, f"new-{i}", device_id=dev, ts="2026-06-20T10:00:00+00:00",
                lat=BASE_LAT + (i - 1) * 0.00003,
            )

    await run_cluster_job(db_pool)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT cluster_id, repaired_at FROM asset_cluster ORDER BY cluster_id"
        )
        # Links for the original (repaired) cluster were not rebuilt this run.
        orig_links = await conn.fetchval(
            "SELECT count(*) FROM observation_cluster_link WHERE cluster_id=$1", original
        )

    assert len(rows) == 2  # repaired one preserved + a brand-new cluster
    repaired = [r for r in rows if r["repaired_at"] is not None]
    fresh = [r for r in rows if r["repaired_at"] is None]
    assert len(repaired) == 1 and repaired[0]["cluster_id"] == original
    assert len(fresh) == 1 and fresh[0]["cluster_id"] != original
    assert orig_links == 3  # original membership untouched


async def test_fused_pair_member_without_pothole_class_is_clustered(db_pool):
    async with db_pool.acquire() as conn:
        # Two pothole-classed observations.
        await _insert_pothole(conn, "p1", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "p2", device_id="dev-2", lat=BASE_LAT + 0.00003)
        # A third observation that is NOT classed as pothole, but is in a
        # high-confidence fusion pair → still a clusterable member.
        await insert_observation(
            conn, "fused-obs", device_id="dev-2", lat=BASE_LAT - 0.00003,
        )
        await insert_frame(conn, "frame-1", device_id="dev-2", lat=BASE_LAT - 0.00003)
        await conn.execute(
            "INSERT INTO fusion_pair (event_client_id, frame_client_id, fused_confidence) "
            "VALUES ($1, $2, $3)",
            "fused-obs", "frame-1", 0.8,
        )

    n = await run_cluster_job(db_pool)
    assert n == 1
    async with db_pool.acquire() as conn:
        members = {
            ln["member_id"]
            for ln in await conn.fetch("SELECT member_id FROM observation_cluster_link")
        }
    assert members == {"p1", "p2", "fused-obs"}
