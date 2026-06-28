"""Pydantic models for the frames ingestion endpoint."""

from pydantic import BaseModel, Field, field_validator


class FrameMetadata(BaseModel):
    """Metadata JSON part of the multipart frame upload.

    Field names match the mobile client's PotholeApi.java multipart construction.
    """

    client_id: str = Field(..., min_length=1, max_length=64)
    event_client_id: str | None = Field(
        default=None,
        max_length=64,
        description="Links this frame to a sensor event (Phase 1.5 fusion slot).",
    )
    ts: str = Field(..., description="UTC ISO-8601 timestamp.")
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    device_p_on_device: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="On-device inference probability.",
    )
    model_id: str | None = Field(
        default=None,
        max_length=128,
        description="On-device model identifier (e.g., 'road_gate_stub_v1').",
    )
    detections: list[dict] | None = Field(
        default=None,
        description="Array of YOLO detection objects from on-device inference.",
    )

    @field_validator("ts")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        """Validate ISO-8601 UTC timestamp format."""
        from datetime import datetime

        try:
            ts_str = v.replace("Z", "+00:00") if v.endswith("Z") else v
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                raise ValueError("Timestamp must include timezone")
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid ISO-8601 timestamp: {v}") from e
        return v


class FrameUploadResponse(BaseModel):
    """Response for POST /api/v1/frames.

    The mobile client currently only checks the HTTP status code, but the
    documented contract returns these fields for future use.
    """

    client_id: str
    server_p: float | None = Field(
        default=None,
        description="Server-side inference probability (null until fusion runs).",
    )
    label: str | None = Field(
        default=None,
        description="Server-assigned label (null until fusion runs).",
    )
    model_id: str | None = Field(
        default=None,
        description="Server-side model that produced server_p (null until fusion runs).",
    )
