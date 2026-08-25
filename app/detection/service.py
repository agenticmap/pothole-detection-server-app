"""Detection worker — the scheduled job that runs server-side inference on frames.

Polls asset_frame WHERE detected_at IS NULL (its own flag, independent of the
fusion job's processed_at), runs the configured detector, writes the server_*
columns, and logs device↔server disagreement. Single-flight via advisory lock,
mirroring run_fusion_job / run_cluster_job in app/fusion/service.py.
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from app.config import settings
from app.detection.engine import FrameDetector
from app.detection.registry import get_detector
from app.services.frame_service import load_frame_bytes

logger = logging.getLogger(__name__)

# Distinct from fusion (0x504F54) and cluster (0x504F55) locks.
_DETECTION_LOCK_KEY = 0x504F56  # 'POT' + 2

_SELECT_UNDETECTED_SQL = """
SELECT client_id, jpeg_url, device_probability
FROM asset_frame
WHERE detected_at IS NULL
ORDER BY received_at ASC, client_id ASC
LIMIT $1
"""

_UPDATE_DETECTED_SQL = """
UPDATE asset_frame
SET server_probability = $2, server_model_id = $3, server_detections = $4::jsonb,
    detected_at = now()
WHERE client_id = $1
"""

_INSERT_DISAGREEMENT_SQL = """
INSERT INTO model_disagreement (
    frame_client_id, device_probability, server_probability, delta, server_model_id
)
VALUES ($1, $2, $3, $4, $5)
"""


async def run_detection_job(pool: asyncpg.Pool, detector: FrameDetector | None = None) -> int:
    """Run inference on undetected frames. Returns the number of frames processed.

    `detector` is injectable for tests; in production it defaults to the configured
    backend (get_detector()). A None detector (backend='none') is a no-op.
    """
    detector = detector or get_detector()
    if detector is None:
        logger.info("Detection backend not configured; skipping.")
        return 0

    async with pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _DETECTION_LOCK_KEY)
        if not locked:
            logger.info("Detection job already running; skipping this tick.")
            return 0
        try:
            rows = await conn.fetch(_SELECT_UNDETECTED_SQL, settings.detection_batch_size)
            if not rows:
                return 0

            threshold = settings.detection_disagreement_threshold
            n_done = 0
            for r in rows:
                client_id = r["client_id"]
                device_p = r["device_probability"]
                try:
                    jpeg = await load_frame_bytes(r["jpeg_url"])
                    # to_thread, not a bare call: detect() is synchronous CPU inference
                    # (~0.3 s/frame for YOLO11s on CPU), so running it inline would hold
                    # the event loop for the whole batch — a ~60 s API stall per tick at
                    # detection_batch_size=200, and every request in this worker blocks.
                    result = await asyncio.to_thread(detector.detect, jpeg)
                    server_p = result.probability
                    model_id = result.model_id
                    detections_json = json.dumps(result.detections)
                except Exception as e:  # noqa: BLE001 — one bad frame must not wedge the queue
                    logger.warning("Detection failed for frame %s: %s", client_id, e)
                    server_p, model_id, detections_json = None, None, None

                # Always set detected_at — even on failure — so a permanently-bad
                # file can't be re-polled forever (server_* stay NULL → fusion falls
                # back to device_probability).
                await conn.execute(
                    _UPDATE_DETECTED_SQL, client_id, server_p, model_id, detections_json
                )

                diverged = (
                    server_p is not None
                    and device_p is not None
                    and abs(device_p - server_p) > threshold
                )
                if diverged:
                    await conn.execute(
                        _INSERT_DISAGREEMENT_SQL,
                        client_id, device_p, server_p, abs(device_p - server_p), model_id,
                    )
                n_done += 1

            logger.info("Detection run: %d frames processed.", n_done)
            return n_done
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _DETECTION_LOCK_KEY)
