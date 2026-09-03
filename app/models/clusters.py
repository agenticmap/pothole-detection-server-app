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


class DetectionBox(BaseModel):
    """One detected box, in the pipeline's single coordinate convention.

    Normalized 0..1, corner-origin, FULL-frame. The ROI crop and the letterbox padding
    are already undone before storage (app/detection/onnx_v1.py::_to_detection), so a
    client multiplies by the rendered width/height and draws. Same convention as
    frame_box; the centre-origin YOLO form exists only in export_labeled_frames.py.
    """

    x: float
    y: float
    w: float
    h: float
    confidence: float = 0.0
    label: str | None = None
    class_id: int | None = None


class VlmVerdictItem(BaseModel):
    """The hybrid detector's VLM verdict, lifted out of server_detections.

    Present only under DETECTION_BACKEND=hybrid, where server_probability is a blend
    rather than the detector's own number -- this is what lets an operator tell the
    two apart.

    WARNING: `rationale` is free text from a third-party model, echoed to a browser.
    Render with textContent, never innerHTML.
    """

    is_pothole: bool
    confidence: float
    severity: str | None = None
    rationale: str = ""
    model_id: str = ""


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
    server_boxes: list[DetectionBox] = Field(
        default_factory=list,
        description="Server detector boxes. Normalized 0..1, corner-origin, full-frame.",
    )
    device_boxes: list[DetectionBox] = Field(
        default_factory=list, description="On-device detector boxes. Same convention."
    )
    vlm_verdict: VlmVerdictItem | None = Field(
        default=None, description="Present only when the hybrid backend verified this frame."
    )


class FrameDetailResponse(BaseModel):
    """GET /api/v1/frames/{client_id} — one frame, whether or not it ever paired.

    Nearly ClusterFrameItem, and deliberately NOT that model. ClusterFrameItem describes
    a frame reached *through* a cluster, so its `paired_observation_id` is required --
    every frame in that list paired with a member by definition. This endpoint serves the
    map, whose frames layer includes UNPAIRED frames on purpose: `frameStatus` in the
    dashboard exists precisely to report "scored but unpaired -- it reached no cluster",
    and 98.6% of pothole-classed observations have no coincident frame at all. Reusing
    ClusterFrameItem would have meant either lying with a placeholder id or widening a
    field that is genuinely required in its own context.

    Same anonymity rule as ClusterFrameItem, and for the same reason: no `jpeg_url` and
    no `device_id`. The stored path is "{device_id}/{client_id}.jpg", so one field would
    leak the device. `image_url` is the authenticated API path instead.
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
    # Nullable, unlike ClusterFrameItem's. This is the whole reason for a separate model.
    paired_observation_id: str | None = Field(
        default=None,
        description="The observation this frame paired with, or null if it never paired.",
    )
    fused_confidence: float | None = None
    delta_ms: int | None = Field(default=None, description="Time offset from the paired event.")
    delta_m: float | None = Field(default=None, description="Distance from the paired event.")
    server_boxes: list[DetectionBox] = Field(
        default_factory=list,
        description="Server detector boxes. Normalized 0..1, corner-origin, full-frame.",
    )
    device_boxes: list[DetectionBox] = Field(
        default_factory=list, description="On-device detector boxes. Same convention."
    )
    vlm_verdict: VlmVerdictItem | None = Field(
        default=None, description="Present only when the hybrid backend verified this frame."
    )


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
    # Descriptions copied verbatim from PotholeItem (app/models/potholes.py). The
    # public and staff paths expose the same two columns, and the definition of a
    # "pass" is the paper's unit of corroboration -- it must not drift between them.
    distinct_passes: int = Field(
        default=0,
        ge=0,
        description=(
            "Distinct (device, drive) passes contributing to this pothole. The "
            "paper's unit of corroboration -- one car over the same defect on "
            "three days is three passes and one device."
        ),
    )
    member_span_s: float | None = Field(
        default=None,
        description=(
            "Seconds between the earliest and latest contributing detection. A "
            "span of a few seconds means one drive-past, not corroboration."
        ),
    )
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
