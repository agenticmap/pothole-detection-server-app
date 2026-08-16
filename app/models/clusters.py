"""Pydantic models for the operator dashboard's cluster detail panel (Phase 2.5).

Staff-only, one tier — unlike the potholes read path there is no public variant:
everything here is the municipal product (which observations corroborate a
cluster, which camera frames back them, who marked it repaired).

Anonymity (roadmap §2.11 — device_id is never exposed to non-admin clients):
  * Members carry `device_ref`, a per-cluster ordinal ("A", "B", "C") computed by
    a window function in SQL, so device_id never leaves Postgres. It answers the
    only question an operator has — "three devices, or one device three times?" —
    without handing out a pseudonym that could be joined across clusters to
    reconstruct a driver's route.
  * ClusterFrameItem has NO jpeg_url field, deliberately: the stored path is
    "{device_id}/{client_id}.jpg", so exposing it would leak device_id in one
    field. The image endpoint is keyed on client_id instead.
"""

from pydantic import BaseModel, Field

# ── Members ───────────────────────────────────────────────────────────────────


class ClusterMemberItem(BaseModel):
    """One sensor observation that contributes to this cluster."""

    client_id: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    ts: str | None = Field(default=None, description="ISO-8601 timestamp (device clock).")
    device_ref: str = Field(
        ...,
        description="Per-cluster device label ('A', 'B', …). Not stable across clusters, "
        "and deliberately not derived from device_id.",
    )
    speed_mps: float | None = None
    accuracy_m: float | None = Field(
        default=None, description="GPS horizontal accuracy in metres, or null if unreported."
    )
    sensor_class: str | None = None
    sensor_p_pothole: float | None = None
    sensor_severity: float | None = None
    sensor_is_outlier: bool | None = None
    fused_confidence: float | None = Field(
        default=None, description="Confidence recorded on the cluster membership link."
    )


# ── Frames ────────────────────────────────────────────────────────────────────


class ClusterFrameItem(BaseModel):
    """A camera frame paired to one of this cluster's member observations.

    Reached via fusion_pair, not via asset_frame.event_client_id — see
    app/services/cluster_detail_service.py for why.
    """

    client_id: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    ts: str | None = Field(default=None, description="ISO-8601 timestamp.")
    image_url: str = Field(..., description="Authenticated API path; NOT a storage path.")
    device_probability: float | None = None
    server_probability: float | None = None
    server_model_id: str | None = None
    detected_at: str | None = Field(
        default=None,
        description="When server-side detection scored this frame; null means not yet scored, "
        "which is distinct from a score of 0.",
    )
    paired_observation_id: str = Field(..., description="The member observation it pairs with.")
    fused_confidence: float | None = None
    delta_ms: int | None = Field(default=None, description="Time offset from the paired event.")
    delta_m: float | None = Field(default=None, description="Distance from the paired event.")


# ── Repair history ────────────────────────────────────────────────────────────


class RepairLogItem(BaseModel):
    """One entry in the cluster's repair audit trail."""

    repair_id: str
    action: str = Field(..., description="'repaired' or 'unrepaired'.")
    note: str | None = None
    user_id: str
    user_email: str | None = Field(
        default=None, description="Null if the account has since been deleted."
    )
    at: str = Field(..., description="ISO-8601 timestamp of the action.")


# ── Envelope ──────────────────────────────────────────────────────────────────


class ClusterDetailResponse(BaseModel):
    """GET /api/v1/clusters/{cluster_id} — staff only."""

    cluster_id: str
    asset_type: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    severity: float | None = None
    confidence: float | None = None
    observation_count: int = Field(..., ge=0)
    distinct_devices: int = Field(..., ge=0)
    last_seen: str | None = Field(default=None, description="ISO-8601 timestamp.")
    source: str | None = None
    repaired_at: str | None = Field(
        default=None, description="ISO-8601 timestamp, or null if the defect is still open."
    )
    created_at: str | None = None
    updated_at: str | None = None

    members: list[ClusterMemberItem] = Field(default_factory=list)
    members_truncated: bool = Field(
        default=False, description="True if more members exist than were returned."
    )
    frames: list[ClusterFrameItem] = Field(default_factory=list)
    frames_truncated: bool = Field(
        default=False, description="True if more frames exist than were returned."
    )
    repair_history: list[RepairLogItem] = Field(default_factory=list)
    generated_at: str = Field(..., description="Server time the response was produced (ISO-8601).")


# ── Repair ────────────────────────────────────────────────────────────────────


class RepairRequest(BaseModel):
    """POST /api/v1/clusters/{cluster_id}/repair body."""

    repaired: bool = Field(..., description="True to mark repaired, false to reopen.")
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional free text recorded in the audit trail.",
    )


class RepairResponse(BaseModel):
    """Result of a repair-state change."""

    cluster_id: str
    repaired_at: str | None = Field(
        default=None, description="ISO-8601 timestamp, or null once reopened."
    )
    changed: bool = Field(
        ...,
        description="False when the cluster was already in the requested state; the request "
        "was then a no-op and no audit row was written.",
    )
    repair_id: str | None = Field(
        default=None, description="Audit row id, present only when changed is true."
    )
