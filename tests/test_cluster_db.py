"""Integration tests for the clustering job (require a local Postgres+PostGIS).

Skipped automatically by the db_pool fixture when no database is reachable.
"""

from datetime import datetime

import pytest

from app.fusion.service import run_cluster_job
from tests.conftest import insert_frame, insert_observation

pytestmark = pytest.mark.asyncio

# A point and small offsets around it. 1° lat ≈ 111 km, so ±0.00006° ≈ ±7 m
# (well inside the 25 m eps); +0.0020° ≈ 222 m (a separate location).
BASE_LAT, BASE_LON = 43.6532, -79.3832


async def _insert_pothole(
    conn, client_id, *, device_id="dev-1", ts="2026-05-27T10:30:00+00:00",
    lat=BASE_LAT, lon=BASE_LON, p=0.9, severity=0.5, accuracy_m=None,
):
    """Insert an observation already scored as a (non-outlier) pothole.

    `accuracy_m=None` leaves the column NULL, which the assignment radius resolves
    to the flat CLUSTER_EPS_M — i.e. pre-2026-08-31 behaviour.
    """
    await insert_observation(conn, client_id, device_id=device_id, ts=ts, lat=lat, lon=lon)
    await conn.execute(
        "UPDATE asset_observation SET sensor_class='pothole', sensor_p_pothole=$2, "
        "sensor_severity=$3, sensor_is_outlier=FALSE, scored_at=now(), accuracy_m=$4 "
        "WHERE client_id=$1",
        client_id, p, severity, accuracy_m,
    )


# Metres to degrees of latitude, for laying out fixtures at known separations.
def _lat_offset(metres: float) -> float:
    return metres / 111_320.0


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


async def test_a_lone_detection_forms_its_own_cluster(db_pool):
    """The paper's behaviour, and the reason CLUSTER_MIN_POINTS is now 1.

    "if no cluster was queried from the database, the newly classified data event
    was considered as a new formed cluster and stored in the database" (§4.4).
    At min_points=3 these two isolated detections were discarded as DBSCAN noise
    and appeared on no surface at all — 46% of admitted members on the real data.
    """
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "lone-a", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "lone-b", device_id="dev-1", lat=BASE_LAT + 0.0020)

    assert await run_cluster_job(db_pool) == 2
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT observation_count, distinct_passes FROM asset_cluster ORDER BY cluster_id"
        )
    assert [r["observation_count"] for r in rows] == [1, 1]
    # One detection is one pass. Nothing about this is corroboration.
    assert [r["distinct_passes"] for r in rows] == [1, 1]


async def test_lone_detections_stay_off_the_public_read_path(db_pool, client):
    """What makes min_points=1 safe: the publication gate, not the clustering gate.

    Relaxing the quorum only moved where uncorroborated detections are filtered.
    If this ever fails, single detections are reaching the mobile app as confirmed
    potholes.
    """
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "lone-a", device_id="dev-1", lat=BASE_LAT)
    await run_cluster_job(db_pool)

    bbox = f"{BASE_LON - 0.01},{BASE_LAT - 0.01},{BASE_LON + 0.01},{BASE_LAT + 0.01}"
    resp = await client.get(
        f"/api/v1/potholes?bbox={bbox}&zoom=16", headers={"Accept-Version": "v1"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == [], "an uncorroborated detection must not be published"


async def test_minpoints_still_rejects_noise_when_raised(db_pool, monkeypatch):
    """The DBSCAN noise path still works — it is a setting, not dead code."""
    monkeypatch.setattr(settings, "cluster_min_points", 3)
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "lone-a", device_id="dev-1", lat=BASE_LAT)
        await _insert_pothole(conn, "lone-b", device_id="dev-1", lat=BASE_LAT + 0.0020)

    assert await run_cluster_job(db_pool) == 0
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


# ── Spatiotemporal crowd fusion (Phase 2.2c) ──────────────────────────────────
#
# These assert the new behaviour, so they are skipped when it is switched off —
# otherwise running the suite with the kill-switches set looks like a regression when
# it is actually the documented fallback working.

from app.config import settings  # noqa: E402

requires_spatiotemporal = pytest.mark.skipif(
    not settings.cluster_spatiotemporal_enabled,
    reason="CLUSTER_SPATIOTEMPORAL_ENABLED is off; the legacy mean path is in use",
)
requires_bearing_aware = pytest.mark.skipif(
    not settings.cluster_bearing_aware,
    reason="CLUSTER_BEARING_AWARE is off; directions are not separated",
)


async def _insert_scored(
    conn, client_id, *, device_id, ts, lat=BASE_LAT, lon=BASE_LON,
    probs=None, bearing=90.0, severity=0.5,
):
    """A pothole observation carrying a full class posterior and a heading."""
    import json

    probs = probs or {"pothole": 0.9, "crack": 0.07, "not": 0.03}
    await insert_observation(
        conn, client_id, device_id=device_id, ts=ts, lat=lat, lon=lon, bearing_deg=bearing
    )
    await conn.execute(
        "UPDATE asset_observation SET sensor_class='pothole', sensor_p_pothole=$2, "
        "sensor_severity=$3, sensor_is_outlier=FALSE, scored_at=now(), "
        "sensor_class_probs=$4::jsonb WHERE client_id=$1",
        client_id, probs["pothole"], severity, json.dumps(probs),
    )


@requires_spatiotemporal
async def test_integration_writes_a_class_distribution_and_bearing(db_pool):
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"st{i}", device_id=f"dev-{i}",
                ts=f"2026-05-27T10:3{i}:00+00:00", lat=BASE_LAT + i * 0.00002,
            )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT confidence, class_probs, bearing_deg FROM asset_cluster"
        )

    import json

    probs = json.loads(row["class_probs"])
    assert sum(probs.values()) == pytest.approx(1.0)
    # confidence must be the pothole component of the integrated distribution --
    # that is what the read path, the tiles and the repair workflow all key off.
    assert row["confidence"] == pytest.approx(probs["pothole"])
    assert row["bearing_deg"] == pytest.approx(90.0, abs=0.5)


@requires_spatiotemporal
async def test_recent_central_member_outweighs_stale_distant_one(db_pool):
    """The method's whole point, end to end through the job.

    Two members disagree. The recent one on the centroid says pothole; the older one
    20 m away says not. Under the old avg(confidence) the answer sat halfway; the
    integrated distribution must follow the recent, central evidence.
    """
    async with db_pool.acquire() as conn:
        await _insert_scored(
            conn, "near-new", device_id="dev-a", ts="2026-05-27T10:30:00+00:00",
            probs={"pothole": 0.9, "crack": 0.05, "not": 0.05},
        )
        await _insert_scored(
            conn, "near-new-2", device_id="dev-b", ts="2026-05-27T10:30:05+00:00",
            probs={"pothole": 0.85, "crack": 0.1, "not": 0.05},
        )
        await _insert_scored(
            conn, "far-old", device_id="dev-c", ts="2026-05-10T08:00:00+00:00",
            lat=BASE_LAT + 0.00018,  # ~20 m away
            probs={"pothole": 0.05, "crack": 0.05, "not": 0.9},
        )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        confidence = await conn.fetchval("SELECT confidence FROM asset_cluster")

    plain_mean = (0.9 + 0.85 + 0.05) / 3  # 0.6
    assert confidence > plain_mean, (
        "the stale, distant dissenter should be down-weighted, not averaged in equally"
    )


@requires_bearing_aware
async def test_opposing_bearings_do_not_merge(db_pool):
    """§4.4: two carriageways sit within eps_m of each other but are separate defects."""
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"nb{i}", device_id=f"dev-n{i}",
                ts=f"2026-05-27T10:3{i}:00+00:00",
                lat=BASE_LAT + i * 0.00001, bearing=0.0,
            )
    assert await run_cluster_job(db_pool) == 1

    # A later survey of the opposite carriageway, same place, reversed heading.
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"sb{i}", device_id=f"dev-s{i}",
                ts=f"2026-05-28T10:3{i}:00+00:00",
                lat=BASE_LAT + i * 0.00001, bearing=180.0,
            )
    await run_cluster_job(db_pool)

    async with db_pool.acquire() as conn:
        bearings = [
            r["bearing_deg"]
            for r in await conn.fetch("SELECT bearing_deg FROM asset_cluster ORDER BY cluster_id")
        ]
    assert len(bearings) == 2, f"expected two clusters, one per direction; got {bearings}"


@requires_bearing_aware
async def test_bearing_comparison_is_circular(db_pool):
    """350° and 10° are 20° apart. A non-circular comparison would call it 340°."""
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"w{i}", device_id=f"dev-w{i}",
                ts=f"2026-05-27T10:3{i}:00+00:00",
                lat=BASE_LAT + i * 0.00001, bearing=350.0,
            )
    assert await run_cluster_job(db_pool) == 1

    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"e{i}", device_id=f"dev-e{i}",
                ts=f"2026-05-28T10:3{i}:00+00:00",
                lat=BASE_LAT + i * 0.00001, bearing=10.0,
            )
    await run_cluster_job(db_pool)

    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM asset_cluster")
    assert n == 1, "headings 20° apart should merge, not split"


async def test_members_without_a_posterior_fall_back_to_the_legacy_mean(db_pool):
    """Safe on un-rescored data: no posterior means keep the old behaviour."""
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "L1", device_id="dev-1", p=0.9)
        await _insert_pothole(conn, "L2", device_id="dev-2", p=0.7)
        await _insert_pothole(conn, "L3", device_id="dev-3", p=0.5)

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT confidence, class_probs FROM asset_cluster")

    assert row["class_probs"] is None, "no distribution should be invented"
    assert row["confidence"] == pytest.approx((0.9 + 0.7 + 0.5) / 3)


# ── Member gate consequences of the pairing search (Phase 2.2d) ────────────────


async def _insert_pair(
    conn, event_client_id, frame_client_id, *, fused, cost, primary,
):
    """Write a fusion_pair row directly, bypassing the engine.

    The member gate is what is under test here, not the fusion arithmetic, so the
    fused_confidence values are chosen rather than computed. Both the confidence
    and the cost are set independently on purpose: the whole point of is_primary is
    that the best VIEW and the most agreeable VERDICT are different frames.
    """
    await conn.execute(
        "INSERT INTO fusion_pair (event_client_id, frame_client_id, fused_confidence, "
        "delta_ms, delta_m, match_cost, is_primary) VALUES ($1,$2,$3,0,15.0,$4,$5)",
        event_client_id, frame_client_id, fused, cost, primary,
    )


async def test_the_member_gate_reads_the_primary_frame_not_the_highest_confidence(db_pool):
    """A max() over N correlated views of one pothole cherry-picks.

    One observation, two frames of it. The geometrically better view (lower cost)
    reports 0.55; a poor view -- far outside the lead band -- reports 0.95. The
    pre-2.2d gate took max() and used 0.95. Measured on pothole_db, that max
    inflated the visual term from 0.176 to 0.324 across 346 multi-frame events, and
    the bias grows with the number of frames.
    """
    async with db_pool.acquire() as conn:
        # sensor_p_pothole below the fused values, so the GREATEST() in the member
        # gate resolves to the fusion side and the assertion is about that side.
        await _insert_pothole(conn, "MP1", device_id="dev-1", p=0.10)
        await _insert_pothole(conn, "MP2", device_id="dev-2", p=0.10)
        await _insert_pothole(conn, "MP3", device_id="dev-3", p=0.10)
        for obs in ("MP1", "MP2", "MP3"):
            await insert_frame(conn, f"{obs}-good", device_id="dev-1")
            await insert_frame(conn, f"{obs}-poor", device_id="dev-1")
            await _insert_pair(conn, obs, f"{obs}-good", fused=0.55, cost=0.1, primary=True)
            await _insert_pair(conn, obs, f"{obs}-poor", fused=0.95, cost=9.9, primary=False)

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        confidence = await conn.fetchval("SELECT confidence FROM asset_cluster")

    assert confidence == pytest.approx(0.55), "the primary view, not the loudest frame"


async def test_pairs_without_a_primary_keep_the_legacy_max(db_pool):
    """Inert on un-rescored data: migration 012 backfills nothing.

    Every pair written before Phase 2.2d has is_primary = false, so without the
    COALESCE fallback those observations would lose their fusion confidence
    entirely and silently drop out of clustering.
    """
    async with db_pool.acquire() as conn:
        for i, obs in enumerate(("LP1", "LP2", "LP3")):
            await _insert_pothole(conn, obs, device_id=f"dev-{i}", p=0.10)
            await insert_frame(conn, f"{obs}-f", device_id="dev-1")
            # No primary anywhere, and no cost -- exactly what a pre-2.2d row is.
            await _insert_pair(conn, obs, f"{obs}-f", fused=0.80, cost=None, primary=False)

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        confidence = await conn.fetchval("SELECT confidence FROM asset_cluster")

    assert confidence == pytest.approx(0.80)


async def test_frame_only_members_are_inert_while_the_flag_is_off(db_pool, monkeypatch):
    """Three high-confidence unpaired frames must not form a cluster by default.

    This is the shipped configuration, and it has to stay shipped that way until
    Phase 2.7 measures a threshold: server_probability is NULL on all 2916 real
    frames, so the only input today is the on-device score, whose floor was dropped
    to ~5% mid-collection (p50 0.118).

    Pinned by monkeypatch rather than a skip marker: "off means inert" is the claim
    that keeps unmeasured on-device guesses out of the read path, so it should be
    verified even in a run where the flag happens to be enabled.
    """
    monkeypatch.setattr(settings, "fusion_frame_only_enabled", False)
    async with db_pool.acquire() as conn:
        for i in range(3):
            await insert_frame(
                conn, f"vision-only-{i}", device_id=f"dev-{i}",
                lat=BASE_LAT + i * 0.00002, lon=BASE_LON, device_probability=0.95,
            )

    assert await run_cluster_job(db_pool) == 0
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM asset_cluster") == 0


async def test_frame_only_members_cluster_when_enabled(db_pool, monkeypatch):
    """The arm works; only the default keeps it out of the pipeline."""
    monkeypatch.setattr(settings, "fusion_frame_only_enabled", True)
    async with db_pool.acquire() as conn:
        for i in range(3):
            await insert_frame(
                conn, f"seen-only-{i}", device_id=f"dev-{i}",
                lat=BASE_LAT + i * 0.00002, lon=BASE_LON, device_probability=0.95,
            )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT observation_count, distinct_devices, confidence, severity, "
            "bearing_deg FROM asset_cluster"
        )
        kinds = await conn.fetch(
            "SELECT DISTINCT kind FROM observation_cluster_link ORDER BY kind"
        )

    assert row["observation_count"] == 3
    assert row["distinct_devices"] == 3
    assert row["confidence"] == pytest.approx(0.95)
    # A frame carries no accelerometer magnitude, so it has no severity or heading.
    assert row["severity"] == pytest.approx(0.0)
    assert row["bearing_deg"] is None
    assert [r["kind"] for r in kinds] == ["frame"]


async def test_a_paired_frame_is_not_also_admitted_on_its_own(db_pool, monkeypatch):
    """Otherwise one sighting counts twice: once fused, once as a bare frame."""
    monkeypatch.setattr(settings, "fusion_frame_only_enabled", True)
    async with db_pool.acquire() as conn:
        for i, obs in enumerate(("DP1", "DP2", "DP3")):
            await _insert_pothole(conn, obs, device_id=f"dev-{i}", p=0.9)
            await insert_frame(
                conn, f"{obs}-frame", device_id=f"dev-{i}", device_probability=0.95
            )
            await _insert_pair(conn, obs, f"{obs}-frame", fused=0.9, cost=0.1, primary=True)

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT observation_count FROM asset_cluster")
        kinds = [
            r["kind"]
            for r in await conn.fetch("SELECT DISTINCT kind FROM observation_cluster_link")
        ]

    assert count == 3, "three sightings, not six"
    assert kinds == ["observation"]


# ── Corroboration by passes (the paper's unit of evidence) ───────────────────
#
# Sattar et al. integrate "from multiple users AND/OR multiple passes of any road
# segment", and their own five-survey validation was ONE phone on five different
# days. distinct_devices cannot express that; distinct_passes is what does.


async def test_one_device_on_three_days_is_three_passes(db_pool):
    """The paper's own experimental design, which the server previously scored as 1.

    One phone, one spot, three separate drives. This is three surveys in the
    paper and a single device here — so without distinct_passes it is invisible.
    """
    async with db_pool.acquire() as conn:
        for day in (10, 12, 14):
            await _insert_scored(
                conn, f"pass-{day}", device_id="dev-solo",
                ts=f"2026-05-{day}T10:00:00+00:00",
            )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT distinct_devices, distinct_passes, observation_count, member_span_s "
            "FROM asset_cluster"
        )
    assert row["distinct_devices"] == 1
    assert row["distinct_passes"] == 3
    assert row["observation_count"] == 3
    # Four days apart, so this is unambiguously not one drive-past.
    assert row["member_span_s"] > 3 * 86400


async def test_one_drive_past_is_a_single_pass(db_pool):
    """The negative, and the reason CLUSTER_MIN_POINTS was never corroboration.

    At the measured median 13 m/s, CLUSTER_EPS_M (25 m) is 1.9 seconds of travel,
    so three detections "within 25 m" is one car crossing one rough patch. Every
    cluster on the collected data spanned a median of 2.0 s. That must score as
    ONE pass however many points it contains.
    """
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"burst{i}", device_id="dev-solo",
                ts=f"2026-05-27T10:00:0{i}+00:00",
            )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT distinct_passes, observation_count, member_span_s FROM asset_cluster"
        )
    assert row["distinct_passes"] == 1
    assert row["observation_count"] == 3
    assert row["member_span_s"] < 30


async def test_a_gap_shorter_than_the_threshold_stays_one_pass(db_pool):
    """A pause in traffic is not a new survey.

    Pins the gap semantics: the boundary is CLUSTER_PASS_GAP_MINUTES of silence,
    not a change of day or an arbitrary bucket.
    """
    gap = settings.cluster_pass_gap_minutes
    async with db_pool.acquire() as conn:
        await _insert_scored(conn, "g0", device_id="dev-solo", ts="2026-05-27T10:00:00+00:00")
        # Comfortably inside the gap.
        await _insert_scored(
            conn, "g1", device_id="dev-solo",
            ts=f"2026-05-27T10:{max(gap - 5, 1):02d}:00+00:00",
        )
        await _insert_scored(
            conn, "g2", device_id="dev-solo",
            ts=f"2026-05-27T10:{max(gap - 4, 2):02d}:00+00:00",
        )

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        passes = await conn.fetchval("SELECT distinct_passes FROM asset_cluster")
    assert passes == 1


async def test_pass_key_is_built_from_the_full_timeline_not_the_admitted_members(db_pool):
    """A long stretch of clean road between two potholes is still one drive.

    The pass key is derived from every record the device produced, not from the
    filtered member set — otherwise a drive that went 25 minutes between two
    admitted detections would be miscounted as two surveys, inflating
    corroboration exactly where the data is thinnest.
    """
    async with db_pool.acquire() as conn:
        await _insert_scored(conn, "far0", device_id="dev-solo", ts="2026-05-27T10:00:00+00:00")
        # Not admitted: classed 'not', so it never reaches `members` — but it is
        # on the device's timeline and bridges the gap.
        await insert_observation(
            conn, "bridge", device_id="dev-solo", ts="2026-05-27T10:15:00+00:00",
            lat=BASE_LAT, lon=BASE_LON,
        )
        await conn.execute(
            "UPDATE asset_observation SET sensor_class='not', sensor_p_pothole=0.01, "
            "sensor_is_outlier=FALSE, scored_at=now() WHERE client_id='bridge'"
        )
        await _insert_scored(conn, "far1", device_id="dev-solo", ts="2026-05-27T10:30:00+00:00")
        await _insert_scored(conn, "far2", device_id="dev-solo", ts="2026-05-27T10:31:00+00:00")

    assert await run_cluster_job(db_pool) == 1
    async with db_pool.acquire() as conn:
        passes = await conn.fetchval("SELECT distinct_passes FROM asset_cluster")
    # 30 minutes end to end, but never 20 minutes of silence.
    assert passes == 1


# ── as-of backtesting (the seam scripts/crowd_sweep.py --accumulate uses) ────


async def test_as_of_excludes_later_observations(db_pool):
    """Clustering "as it stood then" is what a survey-by-survey backtest needs.

    Without this, the accumulate mode could only slice by recency (window_days
    counts backwards from now), which is the opposite of "the first k surveys".
    """
    from app.fusion.service import _compute_clusters

    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"early{i}", device_id="dev-1",
                ts=f"2026-05-10T10:0{i}:00+00:00",
            )
        for i in range(3):
            await _insert_scored(
                conn, f"late{i}", device_id="dev-1",
                ts=f"2026-05-20T10:0{i}:00+00:00",
                lat=BASE_LAT + 0.01,
            )

        common = dict(
            window=3650, min_conf=settings.cluster_member_min_confidence,
            eps_m=settings.cluster_eps_m, min_points=settings.cluster_min_points,
            pass_gap_minutes=settings.cluster_pass_gap_minutes,
        )
        everything, n_all, _, _ = await _compute_clusters(conn, **common)
        early_only, n_early, _, _ = await _compute_clusters(
            conn, **common, as_of=datetime.fromisoformat("2026-05-15T00:00:00+00:00")
        )

    assert n_all == 6
    assert n_early == 3
    assert len(everything) == 2
    assert len(early_only) == 1


async def test_as_of_none_does_not_drop_future_timestamps(db_pool):
    """NULL must mean NO cutoff, not "now".

    Regression: the first version COALESCEd the cutoff to now(), which silently
    excluded any observation whose ts_utc was ahead of the server clock. A device
    with a fast clock produces exactly that, and the repair-recurrence test dates
    its detections in the future on purpose — which is how this was caught.
    """
    from app.fusion.service import _compute_clusters

    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"future{i}", device_id="dev-1", ts=f"2099-01-01T10:0{i}:00+00:00"
            )
        clusters, n_members, _, _ = await _compute_clusters(
            conn, window=3650, min_conf=settings.cluster_member_min_confidence,
            eps_m=settings.cluster_eps_m, min_points=settings.cluster_min_points,
            pass_gap_minutes=settings.cluster_pass_gap_minutes,
        )

    assert n_members == 3, "future-dated observations must still be clustered"
    assert len(clusters) == 1


async def test_as_of_none_is_the_production_path(db_pool):
    """None and omitted must be identical, or every scheduled run changes."""
    from app.fusion.service import _compute_clusters

    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"m{i}", device_id="dev-1", ts=f"2026-05-10T10:0{i}:00+00:00"
            )

        common = dict(
            window=3650, min_conf=settings.cluster_member_min_confidence,
            eps_m=settings.cluster_eps_m, min_points=settings.cluster_min_points,
            pass_gap_minutes=settings.cluster_pass_gap_minutes,
        )
        implicit, n_implicit, _, _ = await _compute_clusters(conn, **common)
        explicit, n_explicit, _, _ = await _compute_clusters(conn, **common, as_of=None)

    assert n_implicit == n_explicit == 3
    assert len(implicit) == len(explicit) == 1


async def test_as_of_also_bounds_the_pass_key(db_pool):
    """A later drive must not retroactively change an earlier survey's pass count.

    The pass key is a window function over the device's whole timeline, so if the
    cutoff were applied only to the member gate, a backtest at survey 1 would see
    pass boundaries computed from surveys it is meant not to know about.
    """
    from app.fusion.service import _compute_clusters

    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_scored(
                conn, f"d1-{i}", device_id="dev-1", ts=f"2026-05-10T10:0{i}:00+00:00"
            )
        for i in range(3):
            await _insert_scored(
                conn, f"d2-{i}", device_id="dev-1", ts=f"2026-05-11T10:0{i}:00+00:00"
            )

        common = dict(
            window=3650, min_conf=settings.cluster_member_min_confidence,
            eps_m=settings.cluster_eps_m, min_points=settings.cluster_min_points,
            pass_gap_minutes=settings.cluster_pass_gap_minutes,
        )
        both, _, _, _ = await _compute_clusters(conn, **common)
        day1, _, _, _ = await _compute_clusters(
            conn, **common, as_of=datetime.fromisoformat("2026-05-10T23:00:00+00:00")
        )

    # Same place, so both drives land in one cluster: 2 passes with everything,
    # 1 pass when the second drive has not happened yet.
    assert both[0]["distinct_passes"] == 2
    assert day1[0]["distinct_passes"] == 1


# ── Sweep-wise assignment with an adaptive GPS-accuracy radius (paper §4.3-4.4) ──


async def test_the_radius_follows_each_event_s_gps_accuracy(db_pool):
    """Two detections 20 m apart split or merge purely on their reported accuracy.

    §4.4 buffers each event at 2 sigma of ITS OWN accuracy. Under the old flat
    25 m eps both pairs merged regardless, because the radius ignored the data.
    """
    async with db_pool.acquire() as conn:
        # 2 sigma = 6 m, well short of the 20 m separation.
        await _insert_pothole(conn, "tight-a", ts="2026-05-01T10:00:00+00:00", accuracy_m=3)
        await _insert_pothole(
            conn, "tight-b", ts="2026-05-02T10:00:00+00:00",
            lat=BASE_LAT + _lat_offset(20), accuracy_m=3,
        )
    assert await run_cluster_job(db_pool) == 2

    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE asset_cluster, observation_cluster_link CASCADE")
        await conn.execute("DELETE FROM asset_observation")
        # Same geometry, but 2 sigma = 30 m now spans it.
        await _insert_pothole(conn, "loose-a", ts="2026-05-01T10:00:00+00:00", accuracy_m=15)
        await _insert_pothole(
            conn, "loose-b", ts="2026-05-02T10:00:00+00:00",
            lat=BASE_LAT + _lat_offset(20), accuracy_m=15,
        )
    assert await run_cluster_job(db_pool) == 1


async def test_assignment_does_not_chain(db_pool):
    """Three collinear detections 20 m apart must not become one 40 m cluster.

    This is what DBSCAN did: A joins B, B joins C, so A and C end up together at
    any distance. On the collected data it produced a "single pothole" 124 m long.
    Matching to a CENTROID cannot chain — once A and B merge, their centroid is
    10 m from each, and C at 40 m is out of reach.
    """
    async with db_pool.acquire() as conn:
        for i, day in enumerate((1, 2, 3)):
            await _insert_pothole(
                conn, f"chain-{i}", ts=f"2026-05-0{day}T10:00:00+00:00",
                lat=BASE_LAT + _lat_offset(20 * i), accuracy_m=12,  # 2 sigma = 24 m
            )
    await run_cluster_job(db_pool)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT observation_count FROM asset_cluster ORDER BY observation_count DESC"
        )
    assert rows[0]["observation_count"] < 3, "all three chained into one cluster"


async def test_one_pass_over_one_defect_is_one_member_group(db_pool):
    """Stage 1: a re-triggering drive-past collapses into a single sweep-event.

    Our phone fires repeatedly on one defect — every cluster the old job produced
    spanned a median of 2.0 s. The paper's phone emits one anomaly per defect, so
    collapsing restores the assumption its algorithm is built on.
    """
    async with db_pool.acquire() as conn:
        for i in range(3):
            await _insert_pothole(
                conn, f"burst-{i}", ts=f"2026-05-27T10:00:0{i}+00:00",
                lat=BASE_LAT + _lat_offset(2 * i), accuracy_m=5,
            )
    assert await run_cluster_job(db_pool) == 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT observation_count, distinct_passes FROM asset_cluster"
        )
    assert row["observation_count"] == 3   # every raw row stays linked
    assert row["distinct_passes"] == 1     # but it is one sweep, not three


async def test_the_same_defect_on_three_days_accumulates_passes(db_pool):
    """Stage 2: later sweeps join the cluster earlier sweeps built."""
    async with db_pool.acquire() as conn:
        for day in (10, 12, 14):
            await _insert_pothole(
                conn, f"day-{day}", ts=f"2026-05-{day}T10:00:00+00:00", accuracy_m=5
            )
    assert await run_cluster_job(db_pool) == 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT distinct_passes, distinct_devices FROM asset_cluster")
    assert row["distinct_passes"] == 3
    assert row["distinct_devices"] == 1


async def test_candidates_come_from_prior_sweeps_only(db_pool):
    """The snapshot rule, and the reason it is not just "nearest centroid".

    Two detections in ONE sweep, far enough apart that stage 1 will not collapse
    them (2 sigma = 4 m against a 20 m gap). They must NOT join each other: a
    cluster created during a sweep is not a candidate for that same sweep. Under a
    naive running-centroid pass they would merge as soon as the radius allowed.
    """
    async with db_pool.acquire() as conn:
        await _insert_pothole(
            conn, "same-sweep-a", ts="2026-05-27T10:00:00+00:00", accuracy_m=2
        )
        await _insert_pothole(
            conn, "same-sweep-b", ts="2026-05-27T10:00:30+00:00",
            lat=BASE_LAT + _lat_offset(20), accuracy_m=2,
        )
    assert await run_cluster_job(db_pool) == 2


async def test_null_accuracy_keeps_the_old_flat_radius(db_pool):
    """Rows predating the GPS-quality column must behave exactly as before."""
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "old-a", ts="2026-05-01T10:00:00+00:00", accuracy_m=None)
        await _insert_pothole(
            conn, "old-b", ts="2026-05-02T10:00:00+00:00",
            lat=BASE_LAT + _lat_offset(20), accuracy_m=None,
        )
    # NULL resolves to CLUSTER_EPS_M (25 m), which spans the 20 m gap.
    assert await run_cluster_job(db_pool) == 1


async def test_assignment_is_deterministic(db_pool):
    """Re-running must reproduce the same grouping, byte for byte.

    The job is built around that property — it is what makes a parameter change
    verifiable by replaying history.
    """
    async with db_pool.acquire() as conn:
        for i in range(6):
            await _insert_pothole(
                conn, f"det-{i}", ts=f"2026-05-1{i}T10:00:00+00:00",
                lat=BASE_LAT + _lat_offset(9 * i), accuracy_m=6,
            )
    await run_cluster_job(db_pool)
    async with db_pool.acquire() as conn:
        first = await conn.fetch(
            "SELECT cluster_id, observation_count FROM asset_cluster ORDER BY cluster_id"
        )
    await run_cluster_job(db_pool)
    async with db_pool.acquire() as conn:
        second = await conn.fetch(
            "SELECT cluster_id, observation_count FROM asset_cluster ORDER BY cluster_id"
        )
    assert [dict(r) for r in first] == [dict(r) for r in second]


async def test_adaptive_radius_can_be_switched_off(db_pool, monkeypatch):
    """The flat radius stays reachable for comparison."""
    monkeypatch.setattr(settings, "cluster_adaptive_radius", False)
    async with db_pool.acquire() as conn:
        await _insert_pothole(conn, "flat-a", ts="2026-05-01T10:00:00+00:00", accuracy_m=1)
        await _insert_pothole(
            conn, "flat-b", ts="2026-05-02T10:00:00+00:00",
            lat=BASE_LAT + _lat_offset(20), accuracy_m=1,
        )
    # 2 sigma would be 2 m and split these; the flat 25 m merges them.
    assert await run_cluster_job(db_pool) == 1
