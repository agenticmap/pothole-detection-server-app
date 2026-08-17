"""Vector tile endpoints for the operator dashboard (Phase 2.5).

    GET /api/v1/tiles/clusters/{z}/{x}/{y}.mvt
    GET /api/v1/tiles/observations/{z}/{x}/{y}.mvt

Staff-only. These carry the severity/confidence detail that Phase 2.4 gated
behind authentication, so they sit at the same tier as /potholes/detail — not on
the anonymous public path.

Auth note for the client: a bearer token is required, and how it gets attached
matters. MapLibre's `transformRequest` hook only *shapes* a request — it never
sees the response, so a 401 leaves the tile in `state = 'errored'`, and an
errored tile never retries itself. Refreshing the token afterwards would leave
the map blank until something forced a reload.

The dashboard therefore registers a custom protocol (`addProtocol`) that owns
the fetch, so it can see the 401, refresh, and retry inline. See
dashboard/src/map/tile-protocol.ts.

(An earlier version of this note claimed `transformRequest` is synchronous. It
is not — the type is `(url, resourceType?) => RequestParameters |
Promise<RequestParameters> | undefined` since MapLibre 5.21. The advice to
refresh proactively still holds; the stated reason did not.)
"""

import logging

from fastapi import APIRouter, Path, Query, Response

from app.config import settings
from app.dependencies import DbPool, ViewerOrAbove
from app.services.tile_service import (
    TileFilter,
    render_cluster_tile,
    render_observation_tile,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tiles", tags=["tiles"])

MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

# Slippy-map path params. Range checking is left entirely to validate_tile_coords:
# the x/y bound depends on z (2^z), which FastAPI cannot express declaratively, and
# splitting the checks would return 422 for a bad zoom but 400 for a bad x.
#
# Each parameter gets its OWN Path() instance. FastAPI sets the alias on the object
# it is given, so sharing one instance between x and y makes the second parameter
# overwrite the first's binding and both then read the same path segment — which
# silently serves the wrong tile rather than erroring.
def _tile_param(name: str) -> Path:
    return Path(..., description=f"Tile {name}")


def _tile_response(tile: bytes) -> Response:
    """Wrap MVT bytes with cache headers.

    `private` because the payload is authenticated and must not be held by a
    shared cache. Browser caching still removes most re-fetches during a pan.
    A CDN-friendly variant would need signed tile URLs instead of a bearer
    header; that is a later concern, noted so the header is not mistaken for
    CDN readiness.
    """
    return Response(
        content=tile,
        media_type=MVT_MEDIA_TYPE,
        headers={"Cache-Control": f"private, max-age={settings.tile_cache_seconds}"},
    )


@router.get("/clusters/{z}/{x}/{y}.mvt", response_class=Response)
async def get_cluster_tile(
    pool: DbPool,
    staff: ViewerOrAbove,
    z: int = _tile_param("zoom"),
    x: int = _tile_param("x"),
    y: int = _tile_param("y"),
    asset_type: str = Query(default="pothole"),
    min_devices: int = Query(
        default=0,
        ge=0,
        description=(
            "Corroboration floor. 0 (default) shows single-device candidates, which "
            "the public path hides; set to 2 to match what the mobile app sees."
        ),
    ),
    include_repaired: bool = Query(
        default=False, description="Include clusters already marked repaired."
    ),
    window_days: int = Query(
        default=0, ge=0, description="Only clusters seen in the last N days (0 = config default)."
    ),
    severity_min: float | None = Query(default=None, ge=0.0),
):
    """Confirmed clusters as MVT. Grid-aggregated at or below the aggregate zoom."""
    tile = await render_cluster_tile(
        pool,
        z=z,
        x=x,
        y=y,
        filters=TileFilter(
            asset_type=asset_type,
            min_devices=min_devices,
            include_repaired=include_repaired,
            window_days=window_days,
            severity_min=severity_min,
        ),
    )
    return _tile_response(tile)


@router.get("/observations/{z}/{x}/{y}.mvt", response_class=Response)
async def get_observation_tile(
    pool: DbPool,
    staff: ViewerOrAbove,
    z: int = _tile_param("zoom"),
    x: int = _tile_param("x"),
    y: int = _tile_param("y"),
    asset_type: str = Query(default="pothole"),
    window_days: int = Query(default=0, ge=0),
):
    """Raw sensor observations as MVT. Street-level zooms only."""
    tile = await render_observation_tile(
        pool,
        z=z,
        x=x,
        y=y,
        filters=TileFilter(asset_type=asset_type, window_days=window_days),
    )
    return _tile_response(tile)
