"""Read path for confirmed potholes — two tiers (Phase 2.4).

  GET /api/v1/potholes         (public) → locations only. Anonymous, account-free.
  GET /api/v1/potholes/detail  (staff)  → full severity/confidence/corroboration.
                                          Requires a staff bearer token.

Wire format contract (v1):
  - Header: Accept-Version: v1 (required). Public route takes NO X-Device-Id.
  - Query: bbox=minLon,minLat,maxLon,maxLat (required), zoom (required), since (optional ISO-8601)
  - Response: { items: [...], generated_at, next_since } — see app/models/potholes.py

zoom > 14 returns individual `pothole` items; zoom <= 14 returns grid-aggregated
`cluster` items. Only clusters with distinct_devices >= the configured minimum and
repaired_at IS NULL are surfaced (both tiers).
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import ApiVersion, CurrentStaff, DbPool
from app.models.potholes import PotholesResponse, PublicPotholesResponse
from app.services.cluster_query_service import query_potholes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["potholes"])

# Widest longitude span a bbox may cover. Beyond 180 degrees a PostGIS geography envelope can have
# antipodal corners, where the great-circle edge between them is ambiguous. Exactly 180 is still
# accepted — it was verified to work — so this only rejects genuinely degenerate requests, and no
# real client viewport comes close.
MAX_BBOX_LON_SPAN_DEG = 180.0


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    """Parse and validate a 'minLon,minLat,maxLon,maxLat' bbox string."""
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=400,
            detail="bbox must be 'minLon,minLat,maxLon,maxLat' (4 comma-separated numbers).",
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox values must be numbers.") from None

    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise HTTPException(status_code=400, detail="bbox longitudes must be in [-180, 180].")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise HTTPException(status_code=400, detail="bbox latitudes must be in [-90, 90].")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="bbox min must be strictly less than max.")
    # PostGIS rejects a geography envelope spanning >= 180 degrees of longitude with
    # "Antipodal (180 degrees long) edge detected!" — the great-circle edge between the corners is
    # ambiguous. That surfaced as an unhandled asyncpg error, i.e. an HTTP 500 on a public,
    # unauthenticated endpoint. A real map viewport is never this wide, so treat it as the client
    # error it is rather than letting it reach ST_MakeEnvelope.
    if (max_lon - min_lon) > MAX_BBOX_LON_SPAN_DEG:
        raise HTTPException(
            status_code=400,
            detail=(
                f"bbox longitude span must not exceed {MAX_BBOX_LON_SPAN_DEG:g} degrees; "
                "request a narrower viewport."
            ),
        )
    return min_lon, min_lat, max_lon, max_lat


def _parse_since(since: str | None) -> datetime | None:
    """Validate an optional ISO-8601 'since' query param into a tz-aware datetime."""
    if since is None:
        return None
    try:
        ts_str = since.replace("Z", "+00:00") if since.endswith("Z") else since
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            raise ValueError("since must include a timezone")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid ISO-8601 'since': {since}") from e
    return dt


@router.get("/potholes", response_model=PublicPotholesResponse)
async def get_potholes(
    pool: DbPool,
    version: ApiVersion,
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    zoom: int = Query(..., ge=0, le=28),
    since: str | None = Query(default=None, description="ISO-8601 timestamp for incremental fetch"),
    min_devices: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Corroboration floor: only clusters seen by at least this many distinct "
            "devices. Omitted (the default) uses CLUSTER_MIN_DISTINCT_DEVICES. "
            "Present so the value can be measured rather than assumed -- see "
            "scripts/device_gate_eval.py."
        ),
    ),
    min_passes: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Corroboration floor by distinct (device, drive) passes -- the unit the "
            "source paper integrates over. A cluster is returned if it meets EITHER "
            "this or min_devices. Omitted uses CLUSTER_MIN_DISTINCT_PASSES."
        ),
    ),
):
    """Public: confirmed pothole LOCATIONS within the bbox, aggregated by zoom."""
    min_lon, min_lat, max_lon, max_lat = _parse_bbox(bbox)
    since_dt = _parse_since(since)
    return await query_potholes(
        pool,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        zoom=zoom,
        since=since_dt,
        detail=False,
        min_devices=min_devices,
        min_passes=min_passes,
    )


@router.get("/potholes/detail", response_model=PotholesResponse)
async def get_potholes_detail(
    pool: DbPool,
    version: ApiVersion,
    staff: CurrentStaff,
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    zoom: int = Query(..., ge=0, le=28),
    since: str | None = Query(default=None, description="ISO-8601 timestamp for incremental fetch"),
    min_devices: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Corroboration floor: only clusters seen by at least this many distinct "
            "devices. Omitted (the default) uses CLUSTER_MIN_DISTINCT_DEVICES. "
            "Present so the value can be measured rather than assumed -- see "
            "scripts/device_gate_eval.py."
        ),
    ),
    min_passes: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Corroboration floor by distinct (device, drive) passes -- the unit the "
            "source paper integrates over. A cluster is returned if it meets EITHER "
            "this or min_devices. Omitted uses CLUSTER_MIN_DISTINCT_PASSES."
        ),
    ),
):
    """Staff-only: confirmed potholes WITH severity, confidence, corroboration.

    Requires a valid staff bearer token (see CurrentStaff → 401 otherwise).
    """
    min_lon, min_lat, max_lon, max_lat = _parse_bbox(bbox)
    since_dt = _parse_since(since)
    logger.info("Staff detail read: user=%s org=%s", staff.user_id, staff.org_id)
    return await query_potholes(
        pool,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        zoom=zoom,
        since=since_dt,
        detail=True,
        min_devices=min_devices,
        min_passes=min_passes,
    )
