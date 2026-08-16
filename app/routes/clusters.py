"""Cluster detail + repair endpoints for the operator dashboard (Phase 2.5).

    GET  /api/v1/clusters/{cluster_id}          detail panel payload
    GET  /api/v1/frames/{client_id}/image       the JPEG behind a frame
    POST /api/v1/clusters/{cluster_id}/repair   mark repaired / reopen

Staff-only, like the tile routes. These carry the severity/corroboration detail
that Phase 2.4 gated behind authentication.

Like app/routes/tiles.py, and unlike the mobile-facing v1 routes, these do NOT
take the Accept-Version dependency: the dashboard ships with the server and is
not the versioned mobile client. That is a decision, not an omission.

POST, not PATCH, for the repair action: it is a command with an audit trail
rather than a partial-resource update. (It also avoids widening CORS
allow_methods in app/main.py, but that is a consequence, not the reason.)

Known authorization gap, inherited from the deferred RLS in 005_auth.sql:
asset_cluster has no org_id, so any staff member of any org can repair any
city's clusters. Reads were already global; the write endpoint makes it matter.

Client note for the dashboard: <img src="…/image"> CANNOT send an Authorization
header, so these images must be fetched with the bearer token and turned into
blob URLs — the same class of problem as MapLibre's transformRequest hook.
"""

import asyncio
import logging
import os

import anyio
from fastapi import APIRouter, HTTPException, Path, Query, Response
from fastapi.responses import FileResponse

from app.config import settings
from app.dependencies import DbPool, StaffOrAboveLive, ViewerOrAbove
from app.models.clusters import ClusterDetailResponse, RepairRequest, RepairResponse
from app.services.cluster_detail_service import (
    MAX_FRAMES,
    get_cluster_detail,
    get_frame_storage_url,
)
from app.services.frame_service import (
    download_frame_bytes_supabase,
    resolve_local_frame_path,
)
from app.services.repair_service import set_repair_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["clusters"])

# This is the only route doing per-request disk I/O, and nothing else bounds it —
# check_rate_limit is device-keyed and ingest-only. Mirrors tile_service's cap.
_image_semaphore = asyncio.Semaphore(settings.tile_max_concurrency)


@router.get("/clusters/{cluster_id}", response_model=ClusterDetailResponse)
async def get_cluster(
    pool: DbPool,
    staff: ViewerOrAbove,
    cluster_id: str = Path(..., description="cluster_id, e.g. clu_<uuid>"),
    frame_limit: int = Query(
        default=MAX_FRAMES, ge=1, le=MAX_FRAMES, description="Frames to return."
    ),
):
    """Everything the detail panel needs, in one request."""
    detail = await get_cluster_detail(pool, cluster_id=cluster_id, frame_limit=frame_limit)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such cluster.")
    return detail


@router.get("/frames/{client_id}/image", response_class=FileResponse)
async def get_frame_image(
    pool: DbPool,
    staff: ViewerOrAbove,
    client_id: str = Path(..., description="asset_frame.client_id"),
    size: str = Query(
        default="full",
        pattern="^full$",
        description="Only 'full' is served today; the parameter exists so adding "
        "thumbnails later is not a breaking change.",
    ),
):
    """Serve the stored JPEG for a frame.

    The storage path is looked up from the database — never taken from the
    client — and still passed through the containment check in frame_service,
    because jpeg_url is an unconstrained TEXT column and a bad row must not
    become an authenticated arbitrary-file read.
    """
    jpeg_url = await get_frame_storage_url(pool, client_id=client_id)
    if jpeg_url is None:
        raise HTTPException(status_code=404, detail="No such frame.")

    # Frames are immutable (ON CONFLICT DO NOTHING at ingest), so they can be
    # cached hard. `private` because the response is authenticated.
    headers = {"Cache-Control": "private, max-age=86400, immutable"}

    if settings.storage_backend == "supabase":
        # load_frame_bytes' supabase branch is a synchronous network call; off the
        # event loop it goes, or one slow download stalls every other request.
        async with _image_semaphore:
            try:
                data = await asyncio.to_thread(download_frame_bytes_supabase, jpeg_url)
            except Exception as exc:  # noqa: BLE001 — any storage failure is a 404 to the client
                logger.warning("Frame %s could not be fetched from storage: %s", client_id, exc)
                raise HTTPException(status_code=404, detail="Frame image unavailable.") from exc
        return Response(content=data, media_type="image/jpeg", headers=headers)

    try:
        path = resolve_local_frame_path(jpeg_url)
    except ValueError as exc:
        logger.warning("Frame %s has a jpeg_url outside the storage root.", client_id)
        raise HTTPException(status_code=404, detail="Frame image unavailable.") from exc

    async with _image_semaphore:
        exists = await anyio.to_thread.run_sync(os.path.isfile, path)
    if not exists:
        # Starlette's FileResponse raises RuntimeError (-> 500) on a missing file,
        # so the existence check has to happen here.
        logger.warning("Frame %s row exists but %s is missing on disk.", client_id, path)
        raise HTTPException(status_code=404, detail="Frame image unavailable.")

    # media_type explicitly: guess_type would fall back to text/plain for a
    # jpeg_url without a .jpg suffix. No filename= — that would flip
    # content_disposition_type to "attachment" and download instead of render.
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.post("/clusters/{cluster_id}/repair", response_model=RepairResponse)
async def repair_cluster(
    pool: DbPool,
    staff: StaffOrAboveLive,
    body: RepairRequest,
    cluster_id: str = Path(..., description="cluster_id, e.g. clu_<uuid>"),
):
    """Mark a cluster repaired, or reopen it.

    Idempotent in the strict sense: repeating the same state is a no-op that
    writes no audit row and — critically — does not re-stamp repaired_at. See
    app/services/repair_service.py for why re-stamping would destroy data.
    """
    outcome = await set_repair_state(
        pool,
        cluster_id=cluster_id,
        repaired=body.repaired,
        note=body.note,
        user_id=staff.user_id,
        org_id=staff.org_id,
    )
    if not outcome.found:
        raise HTTPException(status_code=404, detail="No such cluster.")

    return RepairResponse(
        cluster_id=cluster_id,
        repaired_at=outcome.repaired_at.isoformat() if outcome.repaired_at else None,
        changed=outcome.changed,
        repair_id=outcome.repair_id,
    )
