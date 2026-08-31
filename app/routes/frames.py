"""POST /api/v1/frames — Multipart camera frame ingestion endpoint.

Wire format contract (frozen at v1):
  - Header: X-Device-Id (required)
  - Header: Accept-Version: v1 (required)
  - Content-Type: multipart/form-data
  - Part "metadata": application/json with frame metadata
  - Part "frame": image/jpeg binary data
  - Response: { "client_id", "server_p", "label", "model_id" }

Idempotency:
  - INSERT ... ON CONFLICT (client_id) DO NOTHING
  - Duplicate uploads receive 200 (not 409) — same as the events contract
"""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import settings
from app.dependencies import ApiVersion, DbPool, DeviceId
from app.middleware.rate_limit import check_rate_limit
from app.models.frames import FrameMetadata, FrameUploadResponse
from app.services.frame_service import store_frame

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["frames"])

# JPEG magic bytes: FF D8 FF
_JPEG_MAGIC = b"\xff\xd8\xff"


@router.post("/frames", response_model=FrameUploadResponse)
async def ingest_frame(
    pool: DbPool,
    device_id: DeviceId,
    version: ApiVersion,
    metadata: Annotated[UploadFile | str, File(description="JSON metadata part")],
    frame: Annotated[UploadFile, File(description="JPEG image file")],
):
    """Receive a single camera frame with metadata from a mobile device.

    The mobile client sends one frame per request (not batched). Each frame
    consists of a JSON metadata part and a JPEG binary part.

    The JPEG is stored to the configured storage backend (local filesystem
    or Supabase Storage). Metadata is inserted into the asset_frame table.
    """
    # Rate limit check
    await check_rate_limit(pool, device_id, "frames", count=1)

    # ── Parse metadata JSON ───────────────────────────────────────────────────
    # The metadata part is accepted both with and without a Content-Disposition
    # filename. Starlette only builds an UploadFile when a filename is present;
    # the Android client (OkHttp) sends this part with filename=null, which
    # arrives as a plain string. Both shapes must work — see tests/test_frames.py.
    try:
        # Test for str, not UploadFile: the form parser yields Starlette's
        # UploadFile, which is not an instance of FastAPI's subclass.
        if isinstance(metadata, str):
            metadata_bytes = metadata.encode("utf-8")
        else:
            metadata_bytes = await metadata.read()
        metadata_dict = json.loads(metadata_bytes)
        frame_metadata = FrameMetadata(**metadata_dict)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metadata JSON: {e}",
        )

    # ── Read and validate JPEG ────────────────────────────────────────────────
    jpeg_bytes = await frame.read()

    if len(jpeg_bytes) == 0:
        raise HTTPException(status_code=400, detail="Frame file is empty.")

    if len(jpeg_bytes) > settings.max_frame_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Frame size {len(jpeg_bytes)} bytes exceeds maximum of "
            f"{settings.max_frame_size_bytes} bytes.",
        )

    if not jpeg_bytes[:3] == _JPEG_MAGIC:
        raise HTTPException(
            status_code=400,
            detail="Frame file is not a valid JPEG (invalid magic bytes).",
        )

    # ── Store frame and insert DB record ──────────────────────────────────────
    response = await store_frame(pool, device_id, frame_metadata, jpeg_bytes)

    logger.info(
        "Frame ingested: device=%s client_id=%s size=%d bytes",
        device_id,
        frame_metadata.client_id,
        len(jpeg_bytes),
    )

    return response
