"""Integration tests for the server-side detection worker (require Postgres+PostGIS).

The detector is injected as a StubDetector, so these run without onnxruntime / a real
model — they exercise the job orchestration, server_* writes, disagreement logging,
idempotency, and the failure path.
"""

import json
from pathlib import Path

import pytest

from app.config import settings
from app.detection.engine import DetectionResult
from app.detection.service import run_detection_job
from tests import MINIMAL_JPEG
from tests.conftest import insert_frame

pytestmark = pytest.mark.asyncio


class StubDetector:
    """Deterministic detector for tests — ignores the JPEG, returns fixed output."""

    version = "detection.stub"

    def __init__(self, probability, model_id="stub_v1", detections=None):
        self._p = probability
        self.model_id = model_id
        self._d = detections if detections is not None else []

    def detect(self, jpeg: bytes) -> DetectionResult:
        return DetectionResult(
            probability=self._p, detections=self._d, model_id=self.model_id, version=self.version
        )


def _write_jpeg(device_id: str, client_id: str) -> str:
    """Write a real JPEG to the local store so load_frame_bytes succeeds; return its url."""
    rel = f"{device_id}/{client_id}.jpg"
    path = Path(settings.storage_local_path) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MINIMAL_JPEG)
    return rel


async def test_detection_writes_server_fields(db_pool):
    async with db_pool.acquire() as conn:
        url = _write_jpeg("detdev", "f1")
        await insert_frame(conn, "f1", device_id="detdev", device_probability=0.8, jpeg_url=url)

    n = await run_detection_job(
        db_pool, StubDetector(0.85, detections=[{"conf": 0.85, "xywh": [1, 2, 3, 4]}])
    )
    assert n == 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT server_probability, server_model_id, server_detections, detected_at "
            "FROM asset_frame WHERE client_id='f1'"
        )
        disagreements = await conn.fetchval("SELECT count(*) FROM model_disagreement")

    assert abs(row["server_probability"] - 0.85) < 1e-9
    assert row["server_model_id"] == "stub_v1"
    assert row["detected_at"] is not None
    assert json.loads(row["server_detections"])[0]["conf"] == 0.85
    assert disagreements == 0  # |0.8 - 0.85| < 0.3


async def test_disagreement_is_logged(db_pool):
    async with db_pool.acquire() as conn:
        url = _write_jpeg("detdev", "f2")
        await insert_frame(conn, "f2", device_id="detdev", device_probability=0.4, jpeg_url=url)

    await run_detection_job(db_pool, StubDetector(0.9))

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT frame_client_id, delta, server_model_id FROM model_disagreement"
        )
    assert row["frame_client_id"] == "f2"
    assert abs(row["delta"] - 0.5) < 1e-9  # |0.4 - 0.9|


async def test_rerun_is_idempotent(db_pool):
    async with db_pool.acquire() as conn:
        url = _write_jpeg("detdev", "f3")
        await insert_frame(conn, "f3", device_id="detdev", jpeg_url=url)

    assert await run_detection_job(db_pool, StubDetector(0.7)) == 1
    assert await run_detection_job(db_pool, StubDetector(0.7)) == 0  # nothing left undetected


async def test_inference_error_still_marks_detected(db_pool):
    async with db_pool.acquire() as conn:
        # No file on disk → load_frame_bytes raises → caught, server fields NULL.
        await insert_frame(conn, "f4", device_id="detdev", jpeg_url="detdev/missing.jpg")

    n = await run_detection_job(db_pool, StubDetector(0.9))
    assert n == 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT server_probability, detected_at FROM asset_frame WHERE client_id='f4'"
        )
    assert row["server_probability"] is None
    assert row["detected_at"] is not None  # won't be re-polled


async def test_no_detector_configured_is_noop(db_pool):
    async with db_pool.acquire() as conn:
        url = _write_jpeg("detdev", "f5")
        await insert_frame(conn, "f5", device_id="detdev", jpeg_url=url)
    # backend defaults to "none" in tests → get_detector() returns None → no-op.
    assert await run_detection_job(db_pool) == 0
    async with db_pool.acquire() as conn:
        detected = await conn.fetchval(
            "SELECT detected_at FROM asset_frame WHERE client_id='f5'"
        )
    assert detected is None
