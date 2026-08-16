"""Tests for the operator dashboard's vector tile endpoints (Phase 2.5).

Tiles are asserted by decoding the MVT (tests/mvt.py) rather than by byte length,
so a tile that encodes the wrong layer or drops its attributes fails here.
"""

import math

import pytest

from app.auth.tokens import create_access_token
from app.config import settings
from app.services.tile_service import aggregation_cell_size_m, validate_tile_coords
from tests.mvt import layer_named

# No module-level asyncio mark: pyproject sets asyncio_mode = "auto", and marking
# the module would also tag the synchronous tests in this file.

# The Toronto point the rest of the suite uses.
LAT, LON = 43.6532, -79.3832


def tile_of(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Slippy-map tile containing a coordinate — the client-side convention."""
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def auth(role: str = "staff") -> dict:
    token, _ = create_access_token(user_id="u1", org_id="org_x", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _insert_cluster(
    conn, cluster_id, *, lat=LAT, lon=LON, severity=0.5,
    distinct_devices=2, repaired=False, age_days=0,
):
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, last_seen, source, repaired_at
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            $4, 0.8, 3, $5, now() - make_interval(days => $6), 'crowd',
            CASE WHEN $7 THEN now() ELSE NULL END
        )
        """,
        cluster_id, lon, lat, severity, distinct_devices, age_days, repaired,
    )


def cluster_url(z, x, y, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/api/v1/tiles/clusters/{z}/{x}/{y}.mvt" + (f"?{query}" if query else "")


# ── Coordinate validation (no DB) ─────────────────────────────────────────────

class TestCoordinateValidation:
    def test_valid_coords_pass(self):
        validate_tile_coords(14, 4577, 5970)
        validate_tile_coords(0, 0, 0)

    @pytest.mark.parametrize(
        ("z", "x", "y"),
        [
            (0, 1, 0),      # 2^0 == 1, so x must be 0
            (1, 2, 0),
            (14, 1 << 14, 0),
            (14, 0, 1 << 14),
            (5, -1, 0),
            (-1, 0, 0),
            (23, 0, 0),
        ],
    )
    def test_out_of_range_rejected(self, z, x, y):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            validate_tile_coords(z, x, y)
        # 400, not 422: x/y validity depends on z, so it can't be a declarative check.
        assert exc.value.status_code == 400

    def test_aggregation_cell_shrinks_with_zoom(self):
        assert aggregation_cell_size_m(10) > aggregation_cell_size_m(14)

    async def test_out_of_range_tile_returns_400_not_500(self, client):
        resp = await client.get(cluster_url(2, 99, 0), headers=auth())
        assert resp.status_code == 400


# ── Auth ──────────────────────────────────────────────────────────────────────

class TestTileAuth:
    async def test_unauthenticated_is_401(self, client):
        x, y = tile_of(LON, LAT, 14)
        resp = await client.get(cluster_url(14, x, y))
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    async def test_viewer_may_read_tiles(self, client, db_pool):
        x, y = tile_of(LON, LAT, 14)
        resp = await client.get(cluster_url(14, x, y), headers=auth("viewer"))
        assert resp.status_code == 200

    async def test_unknown_role_denied(self, client):
        """Fail closed — decode_access_token defaults an absent role claim to ""."""
        x, y = tile_of(LON, LAT, 14)
        resp = await client.get(cluster_url(14, x, y), headers=auth(""))
        assert resp.status_code == 403


# ── Tile content ──────────────────────────────────────────────────────────────

class TestClusterTile:
    async def test_empty_area_returns_200_and_empty_body(self, client, db_pool):
        """An empty tile is normal; a 404 would just spam the browser console."""
        x, y = tile_of(LON, LAT, 16)
        resp = await client.get(cluster_url(16, x, y), headers=auth())
        assert resp.status_code == 200
        assert resp.content == b""
        assert resp.headers["content-type"] == "application/vnd.mapbox-vector-tile"

    async def test_seeded_cluster_appears_with_attributes(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_a")

        x, y = tile_of(LON, LAT, 16)
        resp = await client.get(cluster_url(16, x, y), headers=auth())
        assert resp.status_code == 200

        layer = layer_named(resp.content, "clusters")
        assert layer is not None
        assert layer.feature_count == 1
        assert layer.extent == settings.tile_extent
        # The staff tier carries the detail the public path withholds.
        assert {"cluster_id", "severity", "confidence", "distinct_devices"} <= set(layer.keys)

    async def test_cluster_outside_the_tile_is_absent(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_far", lat=1.0, lon=1.0)

        x, y = tile_of(LON, LAT, 16)
        resp = await client.get(cluster_url(16, x, y), headers=auth())
        assert resp.content == b""

    async def test_cache_control_is_private(self, client, db_pool):
        """Authenticated payload — a shared cache must not hold it."""
        x, y = tile_of(LON, LAT, 16)
        resp = await client.get(cluster_url(16, x, y), headers=auth())
        assert resp.headers["cache-control"].startswith("private")

    @pytest.mark.parametrize("z", [0, 1])
    async def test_world_spanning_tiles_do_not_500(self, client, db_pool, z):
        """Regression: a geography envelope at z0/z1 raises 'Antipodal edge detected'.

        Tile SQL stays in 3857 precisely to avoid that, which is also why the
        functional index in migrations/007 is on the transformed geometry.
        """
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_world")
        resp = await client.get(cluster_url(z, 0, 0), headers=auth())
        assert resp.status_code == 200


class TestZoomTiers:
    async def test_low_zoom_aggregates(self, client, db_pool):
        """At or below the aggregate zoom, features are bins — not individual clusters."""
        async with db_pool.acquire() as conn:
            for i in range(5):
                await _insert_cluster(conn, f"clu_agg_{i}", lat=LAT + i * 0.0001)

        z = settings.tile_aggregate_max_zoom
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(cluster_url(z, x, y), headers=auth())

        layer = layer_named(resp.content, "clusters")
        assert layer is not None
        assert "point_count" in layer.keys
        assert "cluster_id" not in layer.keys
        # Five nearby clusters collapse into fewer bins than clusters.
        assert layer.feature_count < 5

    async def test_high_zoom_returns_individuals(self, client, db_pool):
        async with db_pool.acquire() as conn:
            for i in range(3):
                await _insert_cluster(conn, f"clu_ind_{i}", lat=LAT + i * 0.0001)

        z = settings.tile_aggregate_max_zoom + 1
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(cluster_url(z, x, y), headers=auth())

        layer = layer_named(resp.content, "clusters")
        assert layer is not None
        assert "cluster_id" in layer.keys
        assert layer.feature_count == 3


class TestOperatorFilters:
    """The operator tier must show what the public read path deliberately hides."""

    async def test_repaired_excluded_by_default_included_on_request(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_repaired", repaired=True)

        z = 16
        x, y = tile_of(LON, LAT, z)

        default = await client.get(cluster_url(z, x, y), headers=auth())
        assert default.content == b""

        toggled = await client.get(
            cluster_url(z, x, y, include_repaired="true"), headers=auth()
        )
        layer = layer_named(toggled.content, "clusters")
        assert layer is not None and layer.feature_count == 1
        assert "repaired" in layer.keys

    async def test_single_device_cluster_visible_to_operators(self, client, db_pool):
        """The triage queue: the public path hides these, the dashboard needs them."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_solo", distinct_devices=1)

        z = 16
        x, y = tile_of(LON, LAT, z)

        shown = await client.get(cluster_url(z, x, y), headers=auth())
        assert layer_named(shown.content, "clusters").feature_count == 1

        # Raising the floor to the public default hides it again.
        hidden = await client.get(cluster_url(z, x, y, min_devices=2), headers=auth())
        assert hidden.content == b""

    async def test_severity_floor_filters(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_low", severity=0.1)
            await _insert_cluster(conn, "clu_high", severity=4.0, lat=LAT + 0.0001)

        z = 16
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(cluster_url(z, x, y, severity_min=1.0), headers=auth())
        assert layer_named(resp.content, "clusters").feature_count == 1

    async def test_window_days_excludes_stale_clusters(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, "clu_stale", age_days=90)

        z = 16
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(cluster_url(z, x, y), headers=auth())
        assert resp.content == b""

        wide = await client.get(cluster_url(z, x, y, window_days=365), headers=auth())
        assert layer_named(wide.content, "clusters").feature_count == 1


class TestObservationTile:
    async def test_low_zoom_rejected(self, client, db_pool):
        """Unbounded low-zoom scans of the largest table are refused outright."""
        z = settings.tile_observations_min_zoom - 1
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(f"/api/v1/tiles/observations/{z}/{x}/{y}.mvt", headers=auth())
        assert resp.status_code == 400
        assert "zoom" in resp.json()["detail"]

    async def test_high_zoom_allowed(self, client, db_pool):
        z = settings.tile_observations_min_zoom
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(f"/api/v1/tiles/observations/{z}/{x}/{y}.mvt", headers=auth())
        assert resp.status_code == 200

    async def test_unauthenticated_is_401(self, client):
        z = settings.tile_observations_min_zoom
        x, y = tile_of(LON, LAT, z)
        resp = await client.get(f"/api/v1/tiles/observations/{z}/{x}/{y}.mvt")
        assert resp.status_code == 401
