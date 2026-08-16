"""Regression tests for the frame-storage path traversal fix.

Frame JPEGs are stored at "<device_id>/<client_id>.jpg" under
settings.storage_local_path (app/services/frame_service.py::_store_jpeg). Both
components came straight from client input with no charset validation, so a
device id of "../../.." escaped the storage root — an unauthenticated write
primitive, because the ingestion tier is anonymous by design.

These tests pin all three layers of the fix:
  1. X-Device-Id is charset-validated (app/validators.py::is_safe_id),
  2. FrameMetadata.client_id is charset-validated,
  3. _store_jpeg_local refuses to write outside the resolved storage root.

Layer 3 is deliberately redundant with 1 and 2. The path is built by string
interpolation, so it is verified rather than assumed — if a future caller
reaches _store_jpeg_local without going through the route, it still holds.
"""

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.frame_service import _store_jpeg_local
from app.validators import is_safe_id
from tests import MINIMAL_JPEG, make_valid_frame_metadata

# The real Android client sends UUID.randomUUID().toString() for both ids
# (DeviceId.java, EventRepository.java), so the validated charset must admit these.
ANDROID_STYLE_UUID = "2f8e1c4a-0000-4aaa-8bbb-ccccdddd1111"

TRAVERSAL_IDS = [
    "../../..",
    "..",
    "a/../../b",
    "foo/bar",
    "/absolute",
    ".",
]


class TestDeviceIdValidation:
    """X-Device-Id is a path segment; it must not be able to escape."""

    def test_android_uuid_is_accepted_by_the_validator(self):
        """The wire contract must not break: real client ids stay valid."""
        assert is_safe_id(ANDROID_STYLE_UUID)

    @pytest.mark.parametrize("device_id", TRAVERSAL_IDS)
    def test_traversal_shapes_rejected_by_the_validator(self, device_id):
        assert not is_safe_id(device_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("device_id", ["../../..", "foo/bar"])
    async def test_traversal_device_id_rejected_at_ingest(self, client, device_id):
        """A traversing X-Device-Id gets a 400 before any file is written."""
        metadata = json.dumps(make_valid_frame_metadata()).encode()
        response = await client.post(
            "/api/v1/frames",
            headers={"X-Device-Id": device_id, "Accept-Version": "v1"},
            files={
                "metadata": ("metadata.json", metadata, "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        assert response.status_code == 400
        assert "X-Device-Id" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_traversal_device_id_rejected_on_events_too(self, client):
        """The same header guards the events route, which shares the dependency."""
        response = await client.post(
            "/api/v1/events",
            headers={"X-Device-Id": "../../..", "Accept-Version": "v1"},
            json={"events": []},
        )
        assert response.status_code == 400


class TestClientIdValidation:
    """client_id is the filename half of the storage path."""

    @pytest.mark.asyncio
    async def test_traversal_client_id_rejected_at_ingest(self, client):
        metadata = make_valid_frame_metadata()
        metadata["client_id"] = "../../escaped"
        response = await client.post(
            "/api/v1/frames",
            headers={"X-Device-Id": "test-device-uuid-001", "Accept-Version": "v1"},
            files={
                "metadata": ("metadata.json", json.dumps(metadata).encode(), "application/json"),
                "frame": ("frame.jpg", MINIMAL_JPEG, "image/jpeg"),
            },
        )
        # 400 from the route's own metadata validation branch, or 422 from Pydantic.
        assert response.status_code in (400, 422)


class TestStorageContainment:
    """The write itself refuses to leave the storage root, whatever it is handed."""

    @pytest.mark.parametrize("relative_path", ["../escaped.jpg", "../../escaped.jpg"])
    def test_escaping_relative_path_raises(self, relative_path):
        with pytest.raises(ValueError, match="outside the storage root"):
            _store_jpeg_local(relative_path, MINIMAL_JPEG)

    def test_escaping_path_writes_nothing(self, tmp_path):
        """The refusal happens before any mkdir/write, so nothing is left behind."""
        target = Path(settings.storage_local_path).resolve().parent / "escaped.jpg"
        existed = target.exists()
        with pytest.raises(ValueError):
            _store_jpeg_local("../escaped.jpg", MINIMAL_JPEG)
        assert target.exists() == existed

    def test_normal_path_still_writes(self):
        """The guard must not break the happy path."""
        relative = f"{ANDROID_STYLE_UUID}/{ANDROID_STYLE_UUID}.jpg"
        returned = _store_jpeg_local(relative, MINIMAL_JPEG)
        assert returned == relative
        written = Path(settings.storage_local_path).resolve() / relative
        assert written.read_bytes() == MINIMAL_JPEG
        written.unlink()
        written.parent.rmdir()
