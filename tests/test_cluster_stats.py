"""Tests for GET /api/v1/clusters/stats (Phase 2.5b).

The KPI card is a number an operator may put in front of a council. These assert
exact counts against seeded rows rather than "greater than zero", and cover the
boundary cases where a tier bucket could silently take a cluster from the wrong
side of a floor.
"""

import pytest

from app.auth.tokens import create_access_token

# The Toronto point the rest of the suite uses.
LAT, LON = 43.6532, -79.3832

# A box comfortably around it, and one nowhere near it.
BBOX_HERE = "-79.45,43.60,-79.30,43.70"
BBOX_ELSEWHERE = "10.0,50.0,11.0,51.0"

# The dashboard's ramp (dashboard/src/severity.ts). Passed explicitly, because
# the endpoint takes the floors as a parameter rather than owning a second copy.
TIERS = "0,0.25,0.5,0.75"


def auth(role: str = "staff") -> dict:
    token, _ = create_access_token(user_id="u1", org_id="org_x", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _insert_cluster(
    conn,
    cluster_id,
    *,
    lat=LAT,
    lon=LON,
    severity=0.5,
    confidence=0.8,
    repaired=False,
    age_days=0,
):
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, last_seen, source, repaired_at
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            $4, $5, 3, 2, now() - make_interval(days => $6), 'crowd',
            CASE WHEN $7 THEN now() ELSE NULL END
        )
        """,
        cluster_id,
        lon,
        lat,
        severity,
        confidence,
        age_days,
        repaired,
    )


async def _repair_log(conn, cluster_id, *, action="repaired", age_days=0):
    await conn.execute(
        """
        INSERT INTO repair_log (repair_id, cluster_id, action, user_id, created_at)
        VALUES ($1, $2, $3, 'u1', now() - make_interval(days => $4))
        """,
        f"rpr_{cluster_id}_{action}_{age_days}",
        cluster_id,
        action,
        age_days,
    )


def stats_url(bbox=BBOX_HERE, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"bbox": bbox, "tiers": TIERS, **params}.items())
    return f"/api/v1/clusters/stats?{query}"


class TestCounts:
    async def test_open_repaired_and_unrated_are_exact(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", severity=0.1)
            await _insert_cluster(conn, "clu_b", severity=0.6)
            await _insert_cluster(conn, "clu_c", severity=None)
            await _insert_cluster(conn, "clu_d", severity=0.9, repaired=True)

        r = await client.get(stats_url(), headers=auth("viewer"))
        assert r.status_code == 200
        body = r.json()
        assert body["open"] == 3
        assert body["repaired"] == 1
        # Unrated counts OPEN clusters with no score — a repaired one is already
        # counted as repaired, and counting it twice would make the card not add up.
        assert body["unrated"] == 1

    async def test_mean_confidence_covers_open_clusters_only(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", confidence=0.4)
            await _insert_cluster(conn, "clu_b", confidence=0.6)
            # A repaired cluster with a wild value: if it leaked into the mean the
            # assertion below would move.
            await _insert_cluster(conn, "clu_c", confidence=0.99, repaired=True)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["mean_confidence"] == pytest.approx(0.5)

    async def test_mean_confidence_is_null_when_nothing_is_open(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", repaired=True)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["open"] == 0
        # None rather than 0.0: an average of nothing is not zero confidence, and
        # the card must be able to render "—" instead of a made-up number.
        assert body["mean_confidence"] is None

    async def test_empty_viewport_is_all_zeroes_not_an_error(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")

        body = (await client.get(stats_url(bbox=BBOX_ELSEWHERE), headers=auth())).json()
        assert body["open"] == 0
        assert body["tier_counts"] == [0, 0, 0, 0]


class TestTierBucketing:
    async def test_values_land_in_the_tier_they_are_the_floor_of(self, client, db_pool):
        """A value exactly on a floor belongs to that tier, not the one below."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_low", severity=0.0)
            await _insert_cluster(conn, "clu_mod", severity=0.25)
            await _insert_cluster(conn, "clu_high", severity=0.5)
            await _insert_cluster(conn, "clu_sev", severity=0.75)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["tier_counts"] == [1, 1, 1, 1]

    async def test_values_just_below_a_floor_stay_in_the_lower_tier(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", severity=0.2499)
            await _insert_cluster(conn, "clu_b", severity=0.4999)
            await _insert_cluster(conn, "clu_c", severity=0.7499)
            await _insert_cluster(conn, "clu_d", severity=1.0)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["tier_counts"] == [1, 1, 1, 1]

    async def test_unrated_and_repaired_are_not_in_any_tier(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", severity=None)
            await _insert_cluster(conn, "clu_b", severity=0.9, repaired=True)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["tier_counts"] == [0, 0, 0, 0]
        assert body["unrated"] == 1

    async def test_tier_counts_follow_the_caller_s_floors(self, client, db_pool):
        """The ramp lives in the client, so different floors must re-bucket."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", severity=0.3)
            await _insert_cluster(conn, "clu_b", severity=0.8)

        body = (await client.get(stats_url(tiers="0,0.5"), headers=auth())).json()
        assert body["tier_counts"] == [1, 1]


class TestFilters:
    async def test_clusters_outside_the_window_are_excluded(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_recent", age_days=1)
            await _insert_cluster(conn, "clu_stale", age_days=90)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["open"] == 1

    async def test_asset_type_is_respected(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")
            await conn.execute("UPDATE asset_cluster SET asset_type = 'sign'")

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["open"] == 0

    async def test_repaired_last_30d_counts_recent_log_entries_only(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", repaired=True)
            await _insert_cluster(conn, "clu_b", repaired=True)
            await _insert_cluster(conn, "clu_c", repaired=True)
            await _repair_log(conn, "clu_a", age_days=2)
            await _repair_log(conn, "clu_b", age_days=200)
            # An un-repair is not a repair, however recent.
            await _repair_log(conn, "clu_c", action="unrepaired", age_days=1)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["repaired_last_30d"] == 1

    async def test_a_cluster_repaired_twice_counts_once(self, client, db_pool):
        """DISTINCT, not a row count — the card says defects, not log entries."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a", repaired=True)
            await _repair_log(conn, "clu_a", age_days=10)
            await _repair_log(conn, "clu_a", age_days=2)

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["repaired_last_30d"] == 1


class TestValidation:
    @pytest.mark.parametrize(
        "bbox",
        [
            "1,2,3",  # too few
            "a,b,c,d",  # not numbers
            "10,50,5,51",  # min >= max
            "-200,50,-190,51",  # out of range
        ],
    )
    async def test_bad_bbox_is_a_400(self, client, bbox):
        r = await client.get(stats_url(bbox=bbox), headers=auth())
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "tiers",
        [
            "0,0.5,0.25",  # not ascending
            "0,0,0.5",  # not strictly ascending
            "nope",  # not numbers
            "",  # empty
            "0,1,2,3,4,5,6,7,8,9",  # more than MAX_TIERS
        ],
    )
    async def test_bad_tiers_is_a_400(self, client, tiers):
        r = await client.get(stats_url(tiers=tiers), headers=auth())
        assert r.status_code == 400


class TestAuth:
    async def test_anonymous_is_rejected(self, client):
        assert (await client.get(stats_url())).status_code == 401

    async def test_viewer_is_allowed(self, client, db_pool):
        assert (await client.get(stats_url(), headers=auth("viewer"))).status_code == 200


class TestSourceCounts:
    async def test_counts_open_clusters_per_source(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")
            await _insert_cluster(conn, "clu_b")
            await _insert_cluster(conn, "clu_c")
            await conn.execute(
                "UPDATE asset_cluster SET source = 'verified' WHERE cluster_id = 'clu_c'"
            )

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["source_counts"] == {"crowd": 2, "verified": 1}

    async def test_absent_sources_are_omitted_not_zeroed(self, client, db_pool):
        """"No camera-reviewed clusters here" and "camera review: 0" differ."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")

        body = (await client.get(stats_url(), headers=auth())).json()
        assert body["source_counts"] == {"crowd": 1}
        assert "ml" not in body["source_counts"]

    async def test_repaired_clusters_are_excluded(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")
            await _insert_cluster(conn, "clu_b", repaired=True)

        body = (await client.get(stats_url(), headers=auth())).json()
        # The chips filter the open queue, so the counts beside them must agree.
        assert body["source_counts"] == {"crowd": 1}

    async def test_empty_viewport_gives_an_empty_object(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")

        body = (await client.get(stats_url(bbox=BBOX_ELSEWHERE), headers=auth())).json()
        assert body["source_counts"] == {}
