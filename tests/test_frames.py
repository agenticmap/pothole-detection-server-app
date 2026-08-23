"""Integration tests for POST /api/v1/frames endpoint.

These tests validate the multipart wire-format contract.
"""

import json

import pytest

from tests import MINIMAL_JPEG, make_valid_frame_metadata

VALID_HEADERS = {
    "X-Device-Id": "test-device-uuid-001",
    "Accept-Version": "v1",
}


class TestFramesValidation:
    """Test request validation for frame uploads."""

    @pytest.mark.asyncio
    async def test_missing_device_id_returns_400(self, client):
        """X-Device-Id header is required."""
        metadata = json.dumps(make_valid_frame_metadata()).encode()
        response = await client.post(
            "/api/v1/frames",
            headers={"Accept-Version": "v1"},
            files={
                "metadata": ("metadata.json", metadata, "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "X-Device-Id" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_accept_version_returns_400(self, client):
        """Accept-Version header is required."""
        metadata = json.dumps(make_valid_frame_metadata()).encode()
        response = await client.post(
            "/api/v1/frames",
            headers={"X-Device-Id": "test-device"},
            files={
                "metadata": ("metadata.json", metadata, "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "Accept-Version" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_metadata_json_returns_400(self, client):
        """Malformed JSON in metadata part returns 400."""
        response = await client.post(
            "/api/v1/frames",
            headers=VALID_HEADERS,
            files={
                "metadata": ("metadata.json", b"not-json{{{", "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "metadata" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_lat_in_metadata_returns_400(self, client):
        """Latitude validation in frame metadata."""
        meta = make_valid_frame_metadata()
        meta["lat"] = 91.0
        response = await client.post(
            "/api/v1/frames",
            headers=VALID_HEADERS,
            files={
                "metadata": ("metadata.json", json.dumps(meta).encode(), "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_frame_returns_400(self, client):
        """Empty JPEG file is rejected."""
        metadata = json.dumps(make_valid_frame_metadata()).encode()
        response = await client.post(
            "/api/v1/frames",
            headers=VALID_HEADERS,
            files={
                "metadata": ("metadata.json", metadata, "application/json"),
                "frame": ("frame.jpg", b"", "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_non_jpeg_file_returns_400(self, client):
        """Non-JPEG file (wrong magic bytes) is rejected."""
        metadata = json.dumps(make_valid_frame_metadata()).encode()
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # PNG magic
        response = await client.post(
            "/api/v1/frames",
            headers=VALID_HEADERS,
            files={
                "metadata": ("metadata.json", metadata, "application/json"),
                "frame": ("frame.jpg", png_bytes, "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "JPEG" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_oversized_frame_returns_413(self, client):
        """Frame exceeding MAX_FRAME_SIZE_BYTES is rejected."""
        from app.config import settings

        # The route reads settings.max_frame_size_bytes dynamically; mutate the
        # cached settings object (env is read only at import time).
        original = settings.max_frame_size_bytes
        settings.max_frame_size_bytes = 100  # Tiny limit for test
        try:
            metadata = json.dumps(make_valid_frame_metadata()).encode()
            # JPEG magic + garbage to exceed 100 bytes
            big_jpeg = b"\xff\xd8\xff" + b"\x00" * 200
            response = await client.post(
                "/api/v1/frames",
                headers=VALID_HEADERS,
                files={
                    "metadata": ("metadata.json", metadata, "application/json"),
                    "frame": ("frame.jpg", big_jpeg, "image/jpeg"),
                },
            )
            assert response.status_code == 413
        finally:
            settings.max_frame_size_bytes = original

    @pytest.mark.asyncio
    async def test_missing_required_metadata_field_returns_400(self, client):
        """Required fields in metadata must be present."""
        meta = make_valid_frame_metadata()
        del meta["client_id"]
        response = await client.post(
            "/api/v1/frames",
            headers=VALID_HEADERS,
            files={
                "metadata": ("metadata.json", json.dumps(meta).encode(), "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_optional_fields_can_be_omitted(self, client):
        """Optional fields (event_client_id, model_id, detections) may be absent."""
        meta = {
            "client_id": "frame-minimal-001",
            "ts": "2026-05-27T10:30:00Z",
            "lat": 43.6532,
            "lon": -79.3832,
            "device_p_on_device": 0.5,
        }
        # Model-level validation only; this case never reaches the endpoint.
        from app.models.frames import FrameMetadata

        parsed = FrameMetadata(**meta)
        assert parsed.client_id == "frame-minimal-001"
        assert parsed.event_client_id is None
        assert parsed.model_id is None
        assert parsed.detections is None


class TestFramesAndroidWireFormat:
    """Reproduce the real Android client's multipart bytes.

    The rest of this module posts via httpx `files=`, which ALWAYS emits a
    `filename` on every part. The Android client uses OkHttp:

        .addFormDataPart("metadata", null, RequestBody.create(json, JSON))

    and OkHttp omits `; filename=` entirely when the filename is null. Starlette
    only builds an UploadFile when a filename is present, so the metadata part
    arrives as a plain string. Declaring it `metadata: UploadFile` therefore
    rejected every real frame upload with 422 while the whole suite stayed green.

    These tests build the multipart body by hand so the bytes — not the client
    library's conveniences — are what gets asserted on.
    """

    BOUNDARY = "----OkHttpBoundary7MA4YWxkTrZu0gW"

    @classmethod
    def _body(cls, metadata: bytes, *, metadata_filename: str | None) -> bytes:
        """Build a multipart body, optionally omitting the metadata filename."""
        disposition = 'Content-Disposition: form-data; name="metadata"'
        if metadata_filename is not None:
            disposition += f'; filename="{metadata_filename}"'
        return b"".join(
            [
                f"--{cls.BOUNDARY}\r\n{disposition}\r\n"
                f"Content-Type: application/json\r\n\r\n".encode(),
                metadata,
                f'\r\n--{cls.BOUNDARY}\r\nContent-Disposition: form-data; '
                f'name="frame"; filename="frame.jpg"\r\n'
                f"Content-Type: image/jpeg\r\n\r\n".encode(),
                MINIMAL_JPEG,
                f"\r\n--{cls.BOUNDARY}--\r\n".encode(),
            ]
        )

    @classmethod
    def _headers(cls) -> dict:
        return {
            **VALID_HEADERS,
            "Content-Type": f"multipart/form-data; boundary={cls.BOUNDARY}",
        }

    @pytest.mark.asyncio
    async def test_metadata_part_without_filename_is_accepted(self, client):
        """OkHttp shape: metadata part carries NO filename. Must not 422."""
        meta = json.dumps(make_valid_frame_metadata("frame-okhttp-001")).encode()
        response = await client.post(
            "/api/v1/frames",
            headers=self._headers(),
            content=self._body(meta, metadata_filename=None),
        )
        assert response.status_code != 422, (
            "metadata part without a filename was rejected — this is exactly the "
            f"shape the Android client sends: {response.text}"
        )
        assert response.status_code == 200
        assert response.json()["client_id"] == "frame-okhttp-001"

    @pytest.mark.asyncio
    async def test_metadata_part_with_filename_still_accepted(self, client):
        """httpx shape: metadata part HAS a filename. Must keep working."""
        meta = json.dumps(make_valid_frame_metadata("frame-okhttp-002")).encode()
        response = await client.post(
            "/api/v1/frames",
            headers=self._headers(),
            content=self._body(meta, metadata_filename="metadata.json"),
        )
        assert response.status_code == 200
        assert response.json()["client_id"] == "frame-okhttp-002"

    @pytest.mark.asyncio
    async def test_malformed_json_without_filename_returns_400(self, client):
        """A filename-less part with bad JSON still gets the 400 path, not 422."""
        response = await client.post(
            "/api/v1/frames",
            headers=self._headers(),
            content=self._body(b"not-json{{{", metadata_filename=None),
        )
        assert response.status_code == 400


class TestFramesRateLimit:
    """Test rate limiting for frame uploads."""

    @pytest.mark.asyncio
    async def test_frame_rate_limit_enforced(self, client):
        """Device exceeding frames/hour limit gets 429."""
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit, reset_rate_limits

        # check_rate_limit reads settings.rate_limit_frames_per_hour dynamically,
        # so mutate the cached settings object (env is read only at import time).
        original = settings.rate_limit_frames_per_hour
        settings.rate_limit_frames_per_hour = 3
        reset_rate_limits()
        try:
            # First 3 should pass
            for _ in range(3):
                check_rate_limit("frame-rate-device", "frames", count=1)

            # 4th should fail
            with pytest.raises(HTTPException) as exc_info:
                check_rate_limit("frame-rate-device", "frames", count=1)
            assert exc_info.value.status_code == 429
        finally:
            settings.rate_limit_frames_per_hour = original


class TestFrameMetadataScrubbing:
    """Uploaded JPEGs must reach disk without their EXIF (Phase 2.5 security gap)."""

    @staticmethod
    def _jpeg_with_gps() -> bytes:
        import io

        from PIL import Image
        from PIL.TiffImagePlugin import IFDRational

        img = Image.new("RGB", (24, 16), (77, 88, 99))
        exif = img.getexif()
        exif[0x010F] = "UploadedPhoneMaker"
        gps = exif.get_ifd(0x8825)
        gps[1] = "N"
        gps[2] = (IFDRational(43), IFDRational(39), IFDRational(11))
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif, quality=90)
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_stored_jpeg_has_no_exif(self, client, db_pool):
        """The bytes on disk carry no GPS, so no backup or operator view can leak it."""
        from pathlib import Path

        from app.services.frame_service import resolve_local_frame_path

        original = self._jpeg_with_gps()
        assert b"Exif\x00\x00" in original  # the fixture is meaningful

        client_id = "frame-exif-001"
        meta = make_valid_frame_metadata()
        meta["client_id"] = client_id
        response = await client.post(
            "/api/v1/frames",
            headers={"X-Device-Id": "exif-device", "Accept-Version": "v1"},
            files={
                "metadata": ("metadata.json", json.dumps(meta).encode(), "application/json"),
                "frame": ("frame.jpg", original, "image/jpeg"),
            },
        )
        assert response.status_code == 200, response.text

        jpeg_url = response.json().get("jpeg_url")
        async with db_pool.acquire() as conn:
            if not jpeg_url:
                jpeg_url = await conn.fetchval(
                    "SELECT jpeg_url FROM asset_frame WHERE client_id = $1", client_id
                )
        assert jpeg_url

        stored = Path(resolve_local_frame_path(jpeg_url)).read_bytes()
        assert b"Exif\x00\x00" not in stored
        assert b"UploadedPhoneMaker" not in stored
        assert len(stored) < len(original)

        # Still a usable image for the operator dashboard.
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(stored))
        img.load()
        assert img.size == (24, 16)
