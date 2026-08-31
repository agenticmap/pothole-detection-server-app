"""Tests for the public read path GET /api/v1/potholes.

Validation tests need no DB; the rest use the db_pool fixture to seed asset_cluster
(already in the conftest TRUNCATE list) and the httpx client to hit the endpoint.
"""

import pytest

from app.auth.tokens import create_access_token

pytestmark = pytest.mark.asyncio

V = {"Accept-Version": "v1"}

# bbox around the Toronto test point used elsewhere in the suite.
BBOX = "-79.40,43.60,-79.30,43.70"  # minLon,minLat,maxLon,maxLat
IN_LAT, IN_LON = 43.6532, -79.3832
OUT_LAT, OUT_LON = 43.6532, -79.50  # west of minLon → outside bbox


async def _insert_cluster(
    conn, cluster_id, *, lat=IN_LAT, lon=IN_LON, severity=0.5, confidence=0.8,
    observation_count=3, distinct_devices=2, repaired=False, distinct_passes=0,
):
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, last_seen, source, repaired_at,
            distinct_passes
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            $4, $5, $6, $7, now(), 'crowd', CASE WHEN $8 THEN now() ELSE NULL END,
            $9
        )
        """,
        cluster_id, lon, lat, severity, confidence, observation_count,
        distinct_devices, repaired, distinct_passes,
    )


# ── Validation (no DB) ────────────────────────────────────────────────────────

async def test_missing_bbox_returns_422(client):
    resp = await client.get("/api/v1/potholes?zoom=16", headers=V)
    assert resp.status_code == 422


async def test_malformed_bbox_returns_400(client):
    resp = await client.get("/api/v1/potholes?bbox=not,a,bbox&zoom=16", headers=V)
    assert resp.status_code == 400


async def test_out_of_range_bbox_returns_400(client):
    resp = await client.get(
        "/api/v1/potholes?bbox=-79.4,43.6,-79.3,999.0&zoom=16", headers=V
    )
    assert resp.status_code == 400


async def test_missing_version_header_returns_400(client):
    resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16")
    assert resp.status_code == 400


# ── High zoom → individual potholes (DB) ──────────────────────────────────────

async def test_high_zoom_returns_individual_potholes_in_bbox(client, db_pool):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu-a", lat=IN_LAT, lon=IN_LON)
        await _insert_cluster(conn, "clu-b", lat=IN_LAT + 0.001, lon=IN_LON + 0.001)
        await _insert_cluster(conn, "clu-out", lat=OUT_LAT, lon=OUT_LON)  # outside bbox

    resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
    assert resp.status_code == 200
    data = resp.json()
    assert all(it["type"] == "pothole" for it in data["items"])
    ids = {it["id"] for it in data["items"]}
    assert ids == {"clu-a", "clu-b"}  # out-of-bbox excluded
    a = next(it for it in data["items"] if it["id"] == "clu-a")
    assert abs(a["lat"] - IN_LAT) < 1e-6 and abs(a["lon"] - IN_LON) < 1e-6
    assert data["generated_at"] and data["next_since"] == data["generated_at"]


# ── Low zoom → aggregated clusters (DB) ───────────────────────────────────────

async def test_low_zoom_aggregates_nearby_potholes(client, db_pool):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "p1", lat=IN_LAT, lon=IN_LON, severity=0.3)
        await _insert_cluster(conn, "p2", lat=IN_LAT + 0.00005, lon=IN_LON, severity=0.6)
        await _insert_cluster(conn, "p3", lat=IN_LAT, lon=IN_LON + 0.00005, severity=0.5)

    resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=12", headers=V)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    agg = data["items"][0]
    assert agg["type"] == "cluster"
    assert agg["count"] == 3
    # Public tier is locations-only: no max_severity leaks (that's staff-only).
    assert "max_severity" not in agg


async def test_public_individual_omits_detail_fields(client, db_pool):
    """Public /potholes returns id/lat/lon only — never severity/confidence/etc."""
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu-pub", severity=0.7, confidence=0.9, distinct_devices=2)

    resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["type"] == "pothole" and item["id"] == "clu-pub"
    for leaked in ("severity", "confidence", "distinct_devices", "observation_count",
                   "last_seen", "source"):
        assert leaked not in item


# ── Public-visibility filters (DB) ────────────────────────────────────────────

async def test_repaired_and_single_device_clusters_are_excluded(client, db_pool):
    """Corroboration + repair filters on the public read path.

    Pins cluster_min_distinct_devices rather than inheriting it: a deployment may
    legitimately run with 1 (solo test-drive collection), and this test is about
    the filter logic, not about which threshold is configured locally.
    """
    from app.config import settings

    original = settings.cluster_min_distinct_devices
    settings.cluster_min_distinct_devices = 2
    try:
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "good", distinct_devices=2, repaired=False)
            await _insert_cluster(conn, "repaired", distinct_devices=2, repaired=True)
            await _insert_cluster(conn, "single-dev", distinct_devices=1, repaired=False)

        resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert ids == {"good"}
    finally:
        settings.cluster_min_distinct_devices = original


async def test_single_device_clusters_visible_when_threshold_is_one(client, db_pool):
    """Solo-drive mode: with the threshold at 1, one device's cluster goes public.

    This is the configuration a single test driver must run, otherwise
    GET /api/v1/potholes returns [] no matter how much data is collected.
    """
    from app.config import settings

    original = settings.cluster_min_distinct_devices
    settings.cluster_min_distinct_devices = 1
    try:
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "solo", distinct_devices=1, repaired=False)

        resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert "solo" in ids
    finally:
        settings.cluster_min_distinct_devices = original


# ── Degenerate-geometry guards (regression) ───────────────────────────────────

@pytest.mark.parametrize(
    "bbox",
    [
        "-180.0,-10.0,180.0,10.0",   # full 360-degree span
        "-170.0,-10.0,170.0,10.0",   # 340 degrees
        "-100.0,-10.0,90.0,10.0",    # 190 degrees — just over the limit
    ],
)
async def test_over_wide_longitude_span_returns_400(client, bbox):
    """Regression: a bbox wider than 180 degrees used to reach ST_MakeEnvelope.

    PostGIS then raised "Antipodal (180 degrees long) edge detected!", which
    surfaced as an unhandled asyncpg error — an HTTP 500 on a public,
    unauthenticated endpoint. The route must reject it as a client error.
    Compare tests/test_tiles.py::test_world_spanning_tiles_do_not_500, where the
    tile path dodges the same failure by staying in 3857.
    """
    resp = await client.get(f"/api/v1/potholes?bbox={bbox}&zoom=16", headers=V)
    assert resp.status_code == 400
    assert "longitude span" in resp.json()["detail"]


async def test_exactly_180_degree_span_is_allowed(client, db_pool):
    """The boundary is deliberately inclusive — 180 was verified to work."""
    resp = await client.get(
        "/api/v1/potholes?bbox=-90.0,-10.0,90.0,10.0&zoom=16", headers=V
    )
    assert resp.status_code == 200


async def test_postgis_error_becomes_400_not_500(client, db_pool, monkeypatch):
    """Defence in depth: any PostgresError from the read query is a 400, not a 500.

    The route guard above catches the known degenerate bboxes, so this forces the
    service-level except branch directly. A division-by-zero is an
    asyncpg.PostgresError subclass, which is what the handler keys on — the point
    is that no PostGIS complaint about the caller's geometry can reach a public
    client as a server fault.
    """
    from app.services import cluster_query_service

    # Consumes the same nine parameters so asyncpg's arity check still passes.
    # ($8 is min_passes, added alongside min_devices as the second corroboration
    # floor; $9 is the row cap.)
    exploding_sql = """
        SELECT 1 / 0 AS boom
        WHERE $1::int IS NOT NULL AND $2::int IS NOT NULL
          AND $3::float8 IS NOT NULL AND $4::float8 IS NOT NULL
          AND $5::float8 IS NOT NULL AND $6::float8 IS NOT NULL
          AND ($7::timestamptz IS NULL OR TRUE)
          AND $8::int IS NOT NULL AND $9::int IS NOT NULL
    """
    monkeypatch.setattr(cluster_query_service, "_INDIVIDUAL_SQL", exploding_sql)

    resp = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
    assert resp.status_code == 400
    assert "geographic area" in resp.json()["detail"]


# ── min_devices override (Phase 2.7 / device-gate sweep) ─────────────────────
#
# The corroboration floor exists to suppress single-user noise, but the right
# value has never been measured. These pin the parameter that lets it be.


async def test_min_devices_omitted_uses_the_configured_default(db_pool, client):
    """The frozen v1 contract: a client that sends no min_devices sees no change.

    The Android client does not send it. If this ever fails, every deployed
    phone's map has silently changed what it shows.
    """
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_solo", distinct_devices=1)
        await _insert_cluster(conn, "clu_pair", distinct_devices=2)

    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16", headers=V)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    # Default CLUSTER_MIN_DISTINCT_DEVICES is 2, so the single-device one is hidden.
    assert ids == {"clu_pair"}


async def test_min_devices_1_reveals_single_device_clusters(db_pool, client):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_solo", distinct_devices=1)
        await _insert_cluster(conn, "clu_pair", distinct_devices=2)

    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_devices=1", headers=V)
    assert r.status_code == 200
    assert {i["id"] for i in r.json()["items"]} == {"clu_solo", "clu_pair"}


async def test_min_devices_can_tighten_as_well_as_loosen(db_pool, client):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_pair", distinct_devices=2)
        await _insert_cluster(conn, "clu_many", distinct_devices=5)

    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_devices=4", headers=V)
    assert {i["id"] for i in r.json()["items"]} == {"clu_many"}


async def test_min_devices_applies_to_the_aggregate_zoom_too(db_pool, client):
    """Low zoom returns grid bins, and the floor must filter what feeds them.

    Otherwise zooming out would reveal, as a count, exactly the clusters the
    floor hides at street zoom.
    """
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_solo", distinct_devices=1)

    strict = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=10", headers=V)
    assert strict.json()["items"] == []

    loose = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=10&min_devices=1", headers=V)
    items = loose.json()["items"]
    assert len(items) == 1
    assert items[0]["count"] == 1


async def test_min_devices_rejects_negative(client):
    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_devices=-1", headers=V)
    assert r.status_code == 422


async def test_min_devices_on_staff_detail_path(db_pool, client):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_solo", distinct_devices=1)

    token, _ = create_access_token(user_id="u1", org_id="org_x", role="staff")
    auth = {**V, "Authorization": f"Bearer {token}"}
    hidden = await client.get(f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16", headers=auth)
    assert hidden.json()["items"] == []

    shown = await client.get(
        f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16&min_devices=1", headers=auth
    )
    assert [i["id"] for i in shown.json()["items"]] == ["clu_solo"]


# ── Corroboration by passes ─────────────────────────────────────────────────
#
# The paper integrates "from multiple users AND/OR multiple passes of any road
# segment". These pin the second, independent floor.


async def test_a_three_pass_single_device_cluster_is_visible(db_pool, client):
    """One car, three drives — the paper's own experimental design.

    Hidden by the device gate (it will only ever be 1 device) and visible by the
    pass gate. This is the case the server previously could not express at all.
    """
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_3pass", distinct_devices=1, distinct_passes=3)

    hidden = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_passes=99", headers=V)
    assert hidden.json()["items"] == []

    shown = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_passes=3", headers=V)
    assert [i["id"] for i in shown.json()["items"]] == ["clu_3pass"]


async def test_either_floor_is_sufficient(db_pool, client):
    """The two floors are independent, not conjunctive.

    A multi-device cluster stays visible however the pass floor is set, and vice
    versa — otherwise adding the pass gate would have silently *narrowed* the
    existing public contract.
    """
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_devices", distinct_devices=2, distinct_passes=0)
        await _insert_cluster(
            conn, "clu_passes", lat=IN_LAT + 0.001, distinct_devices=1, distinct_passes=5
        )

    r = await client.get(
        f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_devices=2&min_passes=5", headers=V
    )
    assert {i["id"] for i in r.json()["items"]} == {"clu_devices", "clu_passes"}


async def test_pass_floor_still_excludes_a_single_drive_past(db_pool, client):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_1pass", distinct_devices=1, distinct_passes=1)

    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_passes=3", headers=V)
    assert r.json()["items"] == []


async def test_staff_detail_surfaces_passes_and_span(db_pool, client):
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_3pass", distinct_devices=1, distinct_passes=3)
        await conn.execute(
            "UPDATE asset_cluster SET member_span_s = 345600 WHERE cluster_id = 'clu_3pass'"
        )

    token, _ = create_access_token(user_id="u1", org_id="org_x", role="staff")
    auth = {**V, "Authorization": f"Bearer {token}"}
    r = await client.get(
        f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16&min_passes=3", headers=auth
    )
    item = r.json()["items"][0]
    assert item["distinct_passes"] == 3
    assert item["member_span_s"] == 345600


async def test_public_tier_still_returns_locations_only(db_pool, client):
    """Adding fields to the staff model must not leak them into the public tier."""
    async with db_pool.acquire() as conn:
        await _insert_cluster(conn, "clu_3pass", distinct_devices=1, distinct_passes=3)

    r = await client.get(f"/api/v1/potholes?bbox={BBOX}&zoom=16&min_passes=3", headers=V)
    item = r.json()["items"][0]
    assert set(item) == {"type", "id", "lat", "lon"}
