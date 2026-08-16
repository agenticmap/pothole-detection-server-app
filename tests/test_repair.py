"""Tests for POST /api/v1/clusters/{id}/repair (Phase 2.5).

Two of these guard behaviour that is easy to break and silent when broken:

  * test_repeat_repair_does_not_advance_repaired_at — re-stamping repaired_at
    would move the clustering job's exclusion window forward and retroactively
    swallow observations recorded between the two calls, i.e. a double-click
    would erase the evidence that the defect came back.
  * test_repair_committed_mid_run_does_not_resurrect — the TOCTOU between the
    clustering job's compute phase and its write transaction.
"""

import pytest

import app.fusion.service as fusion_service
from app.fusion.service import run_cluster_job
from tests.conftest import insert_observation
from tests.test_tiles import auth, tile_of

pytestmark = pytest.mark.usefixtures("db_pool")

LAT, LON = 43.6532, -79.3832
REPAIR_URL = "/api/v1/clusters/clu_a/repair"


async def _insert_cluster(conn, cluster_id="clu_a", *, lat=LAT, lon=LON, repaired=False):
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, last_seen, source, repaired_at
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            2.5, 0.9, 3, 2, now(), 'crowd',
            CASE WHEN $4 THEN now() ELSE NULL END
        )
        """,
        cluster_id, lon, lat, repaired,
    )


async def _make_staff(conn, *, user_id="u1", org_id="org_x", role="staff"):
    """The live role check reads org_member, so the membership must exist."""
    await conn.execute(
        "INSERT INTO org (org_id, name) VALUES ($1, $1) ON CONFLICT DO NOTHING", org_id
    )
    await conn.execute(
        "INSERT INTO staff_user (user_id, email, password_hash) VALUES ($1, $2, 'x') "
        "ON CONFLICT DO NOTHING",
        user_id, f"{user_id}@example.com",
    )
    await conn.execute(
        "INSERT INTO org_member (org_id, user_id, role) VALUES ($1, $2, $3) "
        "ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role",
        org_id, user_id, role,
    )


# ── Authorization ─────────────────────────────────────────────────────────────

class TestRepairAuth:
    async def test_anonymous_is_401(self, client, db_pool):
        resp = await client.post(REPAIR_URL, json={"repaired": True})
        assert resp.status_code == 401

    async def test_viewer_is_403(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _make_staff(conn, role="viewer")
            await _insert_cluster(conn)
        resp = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth("viewer"))
        assert resp.status_code == 403

    async def test_staff_may_repair(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _make_staff(conn)
            await _insert_cluster(conn)
        resp = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        assert resp.status_code == 200

    async def test_demoted_user_is_rejected_despite_a_valid_token(self, client, db_pool):
        """The write path re-reads org_member instead of trusting the JWT claim.

        The token still says 'staff' — a login-time snapshot lives up to 30 min.
        """
        async with db_pool.acquire() as conn:
            await _make_staff(conn, role="viewer")   # demoted in the DB
            await _insert_cluster(conn)
        resp = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth("staff"))
        assert resp.status_code == 403

    async def test_user_removed_from_org_is_rejected(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)   # no org_member row at all
        resp = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        assert resp.status_code == 403


# ── Core behaviour ────────────────────────────────────────────────────────────

class TestRepairState:
    @pytest.fixture(autouse=True)
    async def _staff(self, db_pool):
        async with db_pool.acquire() as conn:
            await _make_staff(conn)

    async def test_unknown_cluster_is_404(self, client, db_pool):
        resp = await client.post(
            "/api/v1/clusters/clu_nope/repair", json={"repaired": True}, headers=auth()
        )
        assert resp.status_code == 404

    async def test_marks_repaired_and_writes_one_audit_row(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)

        resp = await client.post(
            REPAIR_URL, json={"repaired": True, "note": "patched"}, headers=auth()
        )
        body = resp.json()
        assert body["changed"] is True
        assert body["repaired_at"] is not None
        assert body["repair_id"].startswith("rpr_")

        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT repaired_at FROM asset_cluster WHERE cluster_id = 'clu_a'"
            ) is not None
            rows = await conn.fetch("SELECT * FROM repair_log WHERE cluster_id = 'clu_a'")
        assert len(rows) == 1
        assert rows[0]["action"] == "repaired"
        assert rows[0]["note"] == "patched"
        assert rows[0]["user_id"] == "u1"
        assert rows[0]["org_id"] == "org_x"
        assert rows[0]["repaired_at"] is not None

    async def test_repeat_repair_does_not_advance_repaired_at(self, client, db_pool):
        """Idempotent means no-op, not re-stamp.

        Re-stamping would move _MEMBERS_CTE's exclusion window forward and
        retroactively exclude observations recorded since the first call.
        """
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)

        first = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        second = await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())

        assert first.json()["changed"] is True
        assert second.json()["changed"] is False
        assert second.json()["repair_id"] is None
        assert first.json()["repaired_at"] == second.json()["repaired_at"]

        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM repair_log WHERE cluster_id = 'clu_a'"
            ) == 1

    async def test_updated_at_is_bumped(self, client, db_pool):
        """Change polling keys on updated_at; without the bump the repair is invisible."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            before = await conn.fetchval(
                "SELECT updated_at FROM asset_cluster WHERE cluster_id = 'clu_a'"
            )

        await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())

        async with db_pool.acquire() as conn:
            after = await conn.fetchval(
                "SELECT updated_at FROM asset_cluster WHERE cluster_id = 'clu_a'"
            )
        assert after > before

    async def test_unrepair_clears_and_appends_a_second_row(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)

        await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        resp = await client.post(REPAIR_URL, json={"repaired": False}, headers=auth())

        assert resp.json()["changed"] is True
        assert resp.json()["repaired_at"] is None

        async with db_pool.acquire() as conn:
            actions = [
                r["action"]
                for r in await conn.fetch(
                    "SELECT action FROM repair_log WHERE cluster_id = 'clu_a' "
                    "ORDER BY created_at, repair_id"
                )
            ]
            assert await conn.fetchval(
                "SELECT repaired_at FROM asset_cluster WHERE cluster_id = 'clu_a'"
            ) is None
        # The first row survives — the audit trail is the point.
        assert actions == ["repaired", "unrepaired"]

    async def test_note_length_is_bounded(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
        resp = await client.post(
            REPAIR_URL, json={"repaired": True, "note": "x" * 2001}, headers=auth()
        )
        assert resp.status_code == 422


# ── Visibility downstream ─────────────────────────────────────────────────────

class TestRepairHidesTheCluster:
    @pytest.fixture(autouse=True)
    async def _staff(self, db_pool):
        async with db_pool.acquire() as conn:
            await _make_staff(conn)

    async def test_disappears_from_the_public_read_path(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
        url = "/api/v1/potholes?bbox=-79.5,43.6,-79.3,43.7&zoom=16"
        v = {"Accept-Version": "v1"}

        assert len((await client.get(url, headers=v)).json()["items"]) == 1
        await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        assert (await client.get(url, headers=v)).json()["items"] == []

    async def test_disappears_from_tiles_unless_explicitly_included(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
        x, y = tile_of(LON, LAT, 16)
        url = f"/api/v1/tiles/clusters/16/{x}/{y}.mvt"

        assert (await client.get(url, headers=auth())).content != b""
        await client.post(REPAIR_URL, json={"repaired": True}, headers=auth())
        assert (await client.get(url, headers=auth())).content == b""
        included = await client.get(f"{url}?include_repaired=true", headers=auth())
        assert included.content != b""

    async def test_history_appears_in_the_detail_panel(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
        await client.post(
            REPAIR_URL, json={"repaired": True, "note": "crew 4"}, headers=auth()
        )

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert len(body["repair_history"]) == 1
        entry = body["repair_history"][0]
        assert entry["action"] == "repaired"
        assert entry["note"] == "crew 4"
        assert entry["user_email"] == "u1@example.com"


# ── Interaction with the clustering job ───────────────────────────────────────

class TestClusteringInteraction:
    """The job is repair-safe; these pin it so a future refactor can't break it quietly."""

    @pytest.fixture(autouse=True)
    async def _staff(self, db_pool):
        async with db_pool.acquire() as conn:
            await _make_staff(conn)

    async def _seed_members(self, conn, *, count=4, lat=LAT, lon=LON):
        for i in range(count):
            cid = f"obs-{i}"
            await insert_observation(
                conn, cid, device_id=f"dev-{i % 2}", lat=lat, lon=lon,
                ts="2026-05-27T10:30:00+00:00",
            )
            await conn.execute(
                "UPDATE asset_observation SET sensor_class = 'pothole', "
                "sensor_p_pothole = 0.9, sensor_severity = 0.5, sensor_is_outlier = FALSE "
                "WHERE client_id = $1",
                cid,
            )

    async def test_repair_survives_the_next_cluster_run(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await self._seed_members(conn)
        assert await run_cluster_job(db_pool) == 1

        async with db_pool.acquire() as conn:
            cluster_id = await conn.fetchval("SELECT cluster_id FROM asset_cluster")
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/repair", json={"repaired": True}, headers=auth()
        )
        assert resp.status_code == 200
        repaired_at = resp.json()["repaired_at"]

        await run_cluster_job(db_pool)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT cluster_id, repaired_at FROM asset_cluster")
        assert len(rows) == 1, "the repaired cluster was duplicated"
        assert rows[0]["repaired_at"].isoformat() == repaired_at

    async def test_repair_committed_mid_run_does_not_resurrect(
        self, client, db_pool, monkeypatch
    ):
        """The TOCTOU: the DBSCAN compute runs before the write transaction opens.

        Without the repaired-covering guard before _INSERT_CLUSTER_SQL, the
        members the operator just closed out come back as a brand new,
        un-repaired cluster — silently and permanently.
        """
        async with db_pool.acquire() as conn:
            await self._seed_members(conn)
        await run_cluster_job(db_pool)

        async with db_pool.acquire() as conn:
            cluster_id = await conn.fetchval("SELECT cluster_id FROM asset_cluster")

        original = fusion_service._compute_clusters

        async def compute_then_repair(conn, **kwargs):
            result = await original(conn, **kwargs)
            # Commit the repair on a *separate* connection, after the members have
            # been computed but before the write transaction opens.
            await client.post(
                f"/api/v1/clusters/{cluster_id}/repair",
                json={"repaired": True},
                headers=auth(),
            )
            return result

        monkeypatch.setattr(fusion_service, "_compute_clusters", compute_then_repair)
        await run_cluster_job(db_pool)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT cluster_id, repaired_at FROM asset_cluster")
        assert len(rows) == 1, "a repaired defect was resurrected as a new cluster"
        assert rows[0]["repaired_at"] is not None

    async def test_new_detection_after_repair_forms_a_fresh_cluster(self, client, db_pool):
        """The repair must not suppress genuinely newer evidence."""
        async with db_pool.acquire() as conn:
            await self._seed_members(conn)
        await run_cluster_job(db_pool)

        async with db_pool.acquire() as conn:
            cluster_id = await conn.fetchval("SELECT cluster_id FROM asset_cluster")
        await client.post(
            f"/api/v1/clusters/{cluster_id}/repair", json={"repaired": True}, headers=auth()
        )

        # Detections dated after the repair: the defect returned.
        async with db_pool.acquire() as conn:
            for i in range(4):
                cid = f"obs-new-{i}"
                await insert_observation(
                    conn, cid, device_id=f"dev-{i % 2}", ts="2027-01-01T10:30:00+00:00"
                )
                await conn.execute(
                    "UPDATE asset_observation SET sensor_class = 'pothole', "
                    "sensor_p_pothole = 0.9, sensor_severity = 0.5, "
                    "sensor_is_outlier = FALSE WHERE client_id = $1",
                    cid,
                )
        await run_cluster_job(db_pool)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT cluster_id, repaired_at FROM asset_cluster ORDER BY created_at"
            )
        assert len(rows) == 2, "a post-repair recurrence should form a new cluster"
        assert rows[0]["repaired_at"] is not None
        assert rows[1]["repaired_at"] is None
