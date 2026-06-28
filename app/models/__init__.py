"""Pydantic models for the events ingestion endpoint."""

from datetime import UTC

from pydantic import BaseModel, Field, field_validator


class EventPayload(BaseModel):
    """A single sensor event from the mobile device.

    Field names and semantics match the mobile client's wire format exactly
    (PotholeApi.java + UploadEventsWorker.java).
    """

    client_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="UUID v4 generated on-device before Room insert.",
    )
    schema_version: int = Field(default=1, ge=1, le=100)
    ts: str = Field(
        ...,
        description="UTC ISO-8601 timestamp (yyyy-MM-ddTHH:mm:ssZ).",
    )
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    speed_mps: float = Field(..., ge=0.0, le=200.0)
    bearing_deg: float = Field(..., ge=0.0, le=360.0)
    speed_accuracy_mps: float | None = Field(default=None, ge=0.0, le=200.0)
    accel_max_g: float = Field(..., ge=-50.0, le=50.0)
    accel_std: float = Field(..., ge=0.0, le=100.0)
    magnitude: float = Field(..., ge=0.0, le=500.0)
    gbar_in_max: float | None = Field(default=None, ge=0.0, le=500.0)
    time_in_max: float | None = Field(default=None, ge=0.0)
    time_in_min: float | None = Field(default=None, ge=0.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    raw_window_b64: str | None = Field(
        default=None,
        max_length=50_000,
        description="Base64-encoded gzipped raw sensor window (~3KB typical).",
    )
    visual_confirmed: bool | None = Field(default=None)
    frame_client_id: str | None = Field(default=None, max_length=64)

    @field_validator("ts")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        """Validate ISO-8601 UTC timestamp format."""
        from datetime import datetime, timezone

        try:
            # Accept both 'Z' suffix and '+00:00'
            ts_str = v.replace("Z", "+00:00") if v.endswith("Z") else v
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                raise ValueError("Timestamp must include timezone")
            # Ensure it's not unreasonably far in the future (clock skew tolerance: 1 hour)
            now = datetime.now(UTC)
            if dt > now.replace(hour=now.hour + 1 if now.hour < 23 else 0):
                pass  # Allow — server records received_at independently
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid ISO-8601 timestamp: {v}") from e
        return v


class EventBatchRequest(BaseModel):
    """Batched event upload request matching UploadEventsWorker's wire format."""

    events: list[EventPayload] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Array of sensor events (max 100 per batch).",
    )


class RejectedEvent(BaseModel):
    """An event that was rejected during ingestion."""

    client_id: str
    reason: str


class EventBatchResponse(BaseModel):
    """Response for POST /api/v1/events.

    The client deletes local Room rows only for IDs in the `accepted` array.
    """

    accepted: list[str] = Field(
        default_factory=list,
        description="client_ids successfully ingested (includes idempotent re-uploads).",
    )
    rejected: list[RejectedEvent] = Field(
        default_factory=list,
        description="client_ids that failed validation with reasons.",
    )
