"""Pydantic models for the operator console's frame review surface.

Staff-only. This is the ground-truth labelling loop that `scripts/label_frames.py`
has carried until now, moved into the console so the 1,041 unlabelled frames scoring
>= 0.30 -- the densest expected pothole yield in the corpus -- can be worked through
at speed. Those labels feed `scripts/export_labeled_frames.py` and are what
`scripts/promote_model.py` judges every candidate model on, so the shapes here are
ground truth, not telemetry.

Two anti-anchoring properties are expressed in the *types*, not just the UI:

  * In `order=blind` the scores and both box lists are `None` / empty, because the
    server does not send them. Hiding a number the browser has been given is not
    blindness -- it is in devtools.
  * `server_boxes` is populated only when the client opts in with
    `include_model_boxes=true`. Showing the labeller where the model looked, before
    they have judged the frame, is the anchoring that Phase 2.7b measured as making
    the detector monotonically worse (recall 0.708 -> 0.431 -> 0.354 across three
    successive labelling passes).
"""

from pydantic import BaseModel, Field

from app.models.clusters import DetectionBox, VlmVerdictItem

# -- Queue ---------------------------------------------------------------------


class HumanBox(BaseModel):
    """A box a human drew. Normalized 0..1, corner-origin, full-frame -- the same
    convention as DetectionBox and as frame_box on disk."""

    class_id: int
    x: float
    y: float
    w: float
    h: float


class ReviewFrameItem(BaseModel):
    """One frame in the review queue."""

    client_id: str
    ts: str | None = Field(default=None, description="ISO-8601 capture time.")
    image_url: str = Field(..., description="Authenticated API path; NOT a storage path.")

    # Null in blind mode because they are not sent, not because they are unknown.
    device_probability: float | None = None
    server_probability: float | None = None

    label: int | None = Field(default=None, description="1 pothole, 0 not, -1 unsure.")
    note: str | None = None
    labeled_by: str | None = Field(
        default=None,
        description="Free text from the CLI, or a usr_<uuid> from the console. Two "
        "namespaces in one column by design -- see migrations/017.",
    )
    labeled_at: str | None = None

    boxed_at: str | None = Field(
        default=None,
        description="Set means a human signed this frame off; the exporter keys on it.",
    )
    boxes_drafted_at: str | None = Field(
        default=None,
        description="Set means boxes were saved, INCLUDING a deliberate empty set. "
        "Drafted but not boxed == an unsubmitted draft, re-adoptable after a reload.",
    )
    human_boxes: list[HumanBox] = Field(default_factory=list)

    # Opt-in via include_model_boxes, and never present in blind mode.
    server_boxes: list[DetectionBox] = Field(default_factory=list)
    device_boxes: list[DetectionBox] = Field(default_factory=list)
    vlm_verdict: VlmVerdictItem | None = None


class ReviewQueueCounts(BaseModel):
    """Progress, scoped to the same band the queue was filtered by."""

    outstanding: int = Field(..., description="Frames still needing this mode's work.")
    done: int = Field(..., description="Frames that have had it.")
    in_band: int = Field(..., description="Total frames matching the band filter.")


class ReviewQueueResponse(BaseModel):
    """GET /api/v1/review/frames"""

    items: list[ReviewFrameItem] = Field(default_factory=list)
    counts: ReviewQueueCounts
    mode: str
    order: str
    review: bool
    blind: bool = Field(..., description="True when scores and boxes were withheld.")
    seed: int = Field(..., description="Echo it back to reproduce a blind ordering.")

    # Served rather than hard-coded in TypeScript. app/detection/classes.py warns that
    # the class list, the model's data.yaml and DETECTION_CLASS_NAMES must agree BY
    # POSITION, and that disagreement means server_probability can be sourced from the
    # wrong class -- which fusion cannot detect because it never sees a class. A
    # hand-copied frontend array would be a fourth artefact to drift.
    classes: list[str] = Field(default_factory=list)
    primary_class_id: int
    region_classes: list[str] = Field(
        default_factory=list, description="Classes drawn as regions; a sliver is mostly asphalt."
    )
    thin_aspect_ratio: float

    generated_at: str


# -- Writes --------------------------------------------------------------------


class VerdictRequest(BaseModel):
    """POST /api/v1/review/frames/{client_id}/verdict"""

    label: int = Field(..., description="1 pothole, 0 not, -1 unsure. -1 is a decision, not a gap.")
    note: str | None = Field(default=None, max_length=200)


class VerdictResponse(BaseModel):
    client_id: str
    label: int
    note: str | None = None
    labeled_by: str
    labeled_at: str


class BoxesRequest(BaseModel):
    """POST /api/v1/review/frames/{client_id}/boxes -- replace-all, saves a DRAFT.

    POST rather than PUT despite the replace-all semantics: app/main.py allows only
    GET/POST/OPTIONS, and widening CORS for a verb is the trade app/routes/clusters.py
    already declined once for the repair write.
    """

    boxes: list[HumanBox] = Field(default_factory=list)


class BoxesResponse(BaseModel):
    client_id: str
    boxes: list[HumanBox] = Field(default_factory=list)
    boxed_at: str | None = Field(default=None, description="Still null: saving is not submitting.")
    thin_warnings: list[str] = Field(
        default_factory=list,
        description="Region-class boxes thinner than THIN_ASPECT_RATIO. "
        "A warning, never a refusal.",
    )


class SubmitRequest(BaseModel):
    """POST /api/v1/review/frames/boxes/submit — sign off a batch of drafts.

    The client sends the ids rather than the server tracking a session, which is
    strictly better than the CLI's in-process draft set: it survives a page reload.
    """

    client_ids: list[str] = Field(..., min_length=1, max_length=200)


class SubmitResponse(BaseModel):
    finalized: int
    already_finalized: int
    skipped_unjudged: list[str] = Field(
        default_factory=list,
        description="Frames with no verdict. Reported rather than silently no-opped.",
    )
    skipped_undrafted: list[str] = Field(
        default_factory=list,
        description="Frames judged but never boxed. Signing one off would assert "
        "'reviewed, genuinely clean' about an image nobody opened, and the exporter "
        "would ship it as a YOLO background image. Refused server-side.",
    )


class UnsubmitResponse(BaseModel):
    cleared: int = Field(..., description="Boxes are kept; only the sign-off marker is cleared.")
