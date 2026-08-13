"""Frame ingestion service — JPEG storage and DB insert."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import asyncpg

from app.config import settings
from app.models.frames import FrameMetadata, FrameUploadResponse

logger = logging.getLogger(__name__)

# SQL for inserting a frame record with idempotency.
_INSERT_FRAME_SQL = """
INSERT INTO asset_frame (
    client_id,
    device_id,
    event_client_id,
    ts_utc,
    geom,
    device_probability,
    device_model_id,
    device_detections,
    jpeg_url
)
VALUES (
    $1, $2, $3,
    $4::timestamptz,
    ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
    $7, $8, $9::jsonb, $10
)
ON CONFLICT (client_id) DO NOTHING
"""


async def store_frame(
    pool: asyncpg.Pool,
    device_id: str,
    metadata: FrameMetadata,
    jpeg_bytes: bytes,
) -> FrameUploadResponse:
    """Store the JPEG file and insert frame metadata into the database.

    Storage backend is determined by settings.storage_backend:
      - "local": Writes to local filesystem (development)
      - "supabase": Uploads to Supabase Storage bucket (production)

    Returns a FrameUploadResponse. Idempotent: duplicate client_ids are
    silently accepted without overwriting the existing file.
    """
    # ── Store JPEG ────────────────────────────────────────────────────────────
    jpeg_url = await _store_jpeg(device_id, metadata.client_id, jpeg_bytes)

    # ── Serialize detections to JSON string for JSONB column ──────────────────
    detections_json: str | None = None
    if metadata.detections:
        detections_json = json.dumps(metadata.detections)

    # asyncpg requires a datetime (not str) for the TIMESTAMPTZ column.
    ts_str = metadata.ts.replace("Z", "+00:00") if metadata.ts.endswith("Z") else metadata.ts
    ts_dt = datetime.fromisoformat(ts_str)

    # ── Insert DB record ──────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_FRAME_SQL,
            metadata.client_id,          # $1
            device_id,                   # $2
            metadata.event_client_id,    # $3
            ts_dt,                       # $4
            metadata.lon,                # $5 (ST_MakePoint takes lon first)
            metadata.lat,                # $6
            metadata.device_p_on_device,  # $7
            metadata.model_id,           # $8
            detections_json,             # $9
            jpeg_url,                    # $10
        )

    return FrameUploadResponse(
        client_id=metadata.client_id,
        server_p=None,
        label=None,
        model_id=None,
    )


async def _store_jpeg(device_id: str, client_id: str, jpeg_bytes: bytes) -> str:
    """Store JPEG bytes and return the storage URL/path.

    The path format is: {device_id}/{client_id}.jpg
    This ensures no collisions across devices and allows easy per-device cleanup.
    """
    relative_path = f"{device_id}/{client_id}.jpg"

    if settings.storage_backend == "supabase":
        return await _store_jpeg_supabase(relative_path, jpeg_bytes)
    else:
        return _store_jpeg_local(relative_path, jpeg_bytes)


def _store_jpeg_local(relative_path: str, jpeg_bytes: bytes) -> str:
    """Store JPEG to local filesystem. Returns the relative path as the URL."""
    storage_root = Path(settings.storage_local_path)
    file_path = storage_root / relative_path

    # Create device directory if needed
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically: write to temp file then rename to avoid partial reads
    temp_path = file_path.with_suffix(".tmp")
    temp_path.write_bytes(jpeg_bytes)
    os.replace(temp_path, file_path)

    return relative_path


async def load_frame_bytes(jpeg_url: str) -> bytes:
    """Read a stored JPEG back by its stored url/path — inverse of _store_jpeg.

    Used by the detection worker to fetch a frame for inference.
      - local:    jpeg_url is the relative path under storage_local_path.
      - supabase: jpeg_url is "{bucket}/{relative_path}".
    """
    if settings.storage_backend == "supabase":
        from supabase import create_client

        supabase = create_client(settings.supabase_url, settings.supabase_service_key)
        bucket, _, relative = jpeg_url.partition("/")
        return supabase.storage.from_(bucket).download(relative)

    return (Path(settings.storage_local_path) / jpeg_url).read_bytes()


async def _store_jpeg_supabase(relative_path: str, jpeg_bytes: bytes) -> str:
    """Upload JPEG to Supabase Storage. Returns the public URL."""
    from supabase import create_client

    supabase = create_client(settings.supabase_url, settings.supabase_service_key)
    bucket = settings.supabase_storage_bucket

    # Upload with upsert=True for idempotency (re-uploads overwrite cleanly)
    supabase.storage.from_(bucket).upload(
        path=relative_path,
        file=jpeg_bytes,
        file_options={"content-type": "image/jpeg", "upsert": "true"},
    )

    # Return the storage path (not the full public URL — the server resolves it)
    return f"{bucket}/{relative_path}"
