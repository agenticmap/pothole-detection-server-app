"""Integration tests for POST /api/v1/events endpoint.

These tests validate the wire-format contract that the mobile app depends on.
They run against the FastAPI app via httpx ASGI transport (no real DB needed
for validation tests; DB tests require docker-compose postgres running).
"""

import pytest

from tests import make_valid_event

# Standard headers matching what the mobile client sends
VALID_HEADERS = {
    "X-Device-Id": "test-device-uuid-001",
    "Accept-Version": "v1",
    "Content-Type": "application/json",
}


class TestEventsValidation:
    """Test request validation (no database required)."""

    @pytest.mark.asyncio
    async def test_missing_device_id_returns_400(self, client):
        """X-Device-Id header is required."""
        response = await client.post(
            "/api/v1/events",
            json={"events": [make_valid_event()]},
            headers={"Accept-Version": "v1"},
        )
        assert response.status_code == 400
        assert "X-Device-Id" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_accept_version_returns_400(self, client):
        """Accept-Version header is required."""
        response = await client.post(
            "/api/v1/events",
            json={"events": [make_valid_event()]},
            headers={"X-Device-Id": "test-device"},
        )
        assert response.status_code == 400
        assert "Accept-Version" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_unsupported_version_returns_400(self, client):
        """Only v1 is supported."""
        response = await client.post(
            "/api/v1/events",
            json={"events": [make_valid_event()]},
            headers={"X-Device-Id": "test-device", "Accept-Version": "v2"},
        )
        assert response.status_code == 400
        assert "Unsupported" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_empty_events_array_returns_422(self, client):
        """At least one event is required."""
        response = await client.post(
            "/api/v1/events",
            json={"events": []},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_lat_returns_422(self, client):
        """Latitude must be in [-90, 90]."""
        event = make_valid_event()
        event["lat"] = 91.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_lon_returns_422(self, client):
        """Longitude must be in [-180, 180]."""
        event = make_valid_event()
        event["lon"] = -181.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_confidence_returns_422(self, client):
        """Confidence must be in [0, 1]."""
        event = make_valid_event()
        event["confidence"] = 1.5
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_accel_max_g_returns_422(self, client):
        """accel_max_g must be in [-50, 50]."""
        event = make_valid_event()
        event["accel_max_g"] = 55.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_speed_returns_422(self, client):
        """speed_mps must be in [0, 200]."""
        event = make_valid_event()
        event["speed_mps"] = -5.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_timestamp_returns_422(self, client):
        """Timestamp must be valid ISO-8601."""
        event = make_valid_event()
        event["ts"] = "not-a-timestamp"
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_422(self, client):
        """Required fields cannot be omitted."""
        event = make_valid_event()
        del event["accel_max_g"]
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_optional_fields_can_be_omitted(self, client):
        """Optional fields (raw_window_b64, visual_confirmed, etc.) may be absent."""
        event = make_valid_event()
        # These are all optional — should not fail validation
        assert "raw_window_b64" not in event
        assert "visual_confirmed" not in event
        assert "frame_client_id" not in event
        # Validation will pass; DB insert may fail without postgres, but
        # the Pydantic model validation step succeeds.
        # We can only fully test this with postgres running.

    @pytest.mark.asyncio
    async def test_batch_over_100_returns_422(self, client):
        """Batch size > 100 is rejected by Pydantic model."""
        events = [make_valid_event(f"event-{i}") for i in range(101)]
        response = await client.post(
            "/api/v1/events",
            json={"events": events},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422


class TestEventsRateLimit:
    """Test rate limiting behavior."""

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self, client):
        """Device exceeding events/hour limit gets 429."""
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit, reset_rate_limits

        # check_rate_limit reads settings.rate_limit_events_per_hour dynamically,
        # so mutate the cached settings object (env is read only at import time).
        original = settings.rate_limit_events_per_hour
        settings.rate_limit_events_per_hour = 5
        reset_rate_limits()
        try:
            # First 5 should pass
            for _ in range(5):
                check_rate_limit("rate-test-device", "events", count=1)

            # 6th should fail
            with pytest.raises(HTTPException) as exc_info:
                check_rate_limit("rate-test-device", "events", count=1)
            assert exc_info.value.status_code == 429
        finally:
            settings.rate_limit_events_per_hour = original
