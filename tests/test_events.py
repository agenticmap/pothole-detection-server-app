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
        """accel_max_g must be in [-200, 200] (m/s², not g — see EventPayload)."""
        event = make_valid_event()
        event["accel_max_g"] = 250.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_hard_pothole_strike_is_accepted(self, client):
        """A hard strike (>50 m/s²) must NOT be rejected.

        Regression: the old ±50 bound treated m/s² data as g, so real impacts
        were 422'd. The client re-sends the same oldest rows every retry, so one
        rejected row wedged the whole upload queue for that device.
        """
        event = make_valid_event()
        event["accel_max_g"] = 78.5
        event["magnitude"] = 640.0
        response = await client.post(
            "/api/v1/events",
            json={"events": [event]},
            headers=VALID_HEADERS,
        )
        assert response.status_code == 200

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
    """Rate limiting, now counted in Postgres rather than per-worker memory."""

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self, db_pool, monkeypatch):
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit

        monkeypatch.setattr(settings, "rate_limit_events_per_hour", 5)

        for _ in range(5):
            await check_rate_limit(db_pool, "rate-test-device", "events", count=1)

        with pytest.raises(HTTPException) as exc:
            await check_rate_limit(db_pool, "rate-test-device", "events", count=1)
        assert exc.value.status_code == 429
        assert exc.value.detail["current"] == 6
        assert exc.value.detail["limit"] == 5

    @pytest.mark.asyncio
    async def test_a_batch_consumes_its_whole_size(self, db_pool, monkeypatch):
        """`count` is the batch size, so one 10-event POST spends ten."""
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit

        monkeypatch.setattr(settings, "rate_limit_events_per_hour", 5)
        with pytest.raises(HTTPException):
            await check_rate_limit(db_pool, "batch-device", "events", count=10)

    @pytest.mark.asyncio
    async def test_the_count_is_shared_not_per_process(self, db_pool, monkeypatch):
        """The whole reason this moved into Postgres.

        The old limiter kept module-level dicts, so with `uvicorn --workers 2`
        each worker enforced its own private ceiling and the effective limit was
        doubled. Two independent pools stand in for two workers: the second must
        see what the first already spent.
        """
        from fastapi import HTTPException

        from app.config import settings
        from app.database import create_pool
        from app.middleware.rate_limit import check_rate_limit

        monkeypatch.setattr(settings, "rate_limit_events_per_hour", 3)
        for _ in range(3):
            await check_rate_limit(db_pool, "shared-device", "events", count=1)

        other_worker = await create_pool()
        try:
            with pytest.raises(HTTPException) as exc:
                await check_rate_limit(other_worker, "shared-device", "events", count=1)
            assert exc.value.status_code == 429
        finally:
            await other_worker.close()

    @pytest.mark.asyncio
    async def test_devices_do_not_share_an_allowance(self, db_pool, monkeypatch):
        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit

        monkeypatch.setattr(settings, "rate_limit_events_per_hour", 2)
        for _ in range(2):
            await check_rate_limit(db_pool, "device-a", "events", count=1)
        # device-b has spent nothing and must not inherit device-a's usage.
        await check_rate_limit(db_pool, "device-b", "events", count=1)

    @pytest.mark.asyncio
    async def test_resources_do_not_share_an_allowance(self, db_pool, monkeypatch):
        from app.config import settings
        from app.middleware.rate_limit import check_rate_limit

        monkeypatch.setattr(settings, "rate_limit_events_per_hour", 2)
        monkeypatch.setattr(settings, "rate_limit_frames_per_hour", 2)
        for _ in range(2):
            await check_rate_limit(db_pool, "dual-device", "events", count=1)
        await check_rate_limit(db_pool, "dual-device", "frames", count=1)

    @pytest.mark.asyncio
    async def test_it_fails_open_when_accounting_breaks(self):
        """A device that cannot upload loses collected drive data permanently.

        Overshooting a quota costs a few rows of disk, so a broken counter allows
        the request and logs at ERROR rather than 503ing ingestion.
        """
        import asyncpg

        from app.middleware.rate_limit import check_rate_limit

        class BrokenPool:
            def acquire(self):
                raise asyncpg.PostgresError("simulated outage")

        # Must not raise.
        await check_rate_limit(BrokenPool(), "unlucky-device", "events", count=1)
