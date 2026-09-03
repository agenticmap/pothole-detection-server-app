"""Frame review endpoints — the labelling loop, moved into the operator console.

    GET  /api/v1/review/frames                            the queue
    POST /api/v1/review/frames/{client_id}/verdict        1 / 0 / -1 + note
    POST /api/v1/review/frames/{client_id}/boxes          replace-all, saves a DRAFT
    POST /api/v1/review/frames/boxes/submit               sign off a batch
    POST /api/v1/review/frames/boxes/unsubmit             retract a sign-off

Why this exists. The detector's binding constraint is a shortage of *in-domain
positives*: every hand-drawn box in the training set to date is a negative, and the
only untried remedy is to label and box our own. 1,041 unlabelled frames sit at score
>= 0.30, the densest expected pothole yield in the corpus. Doing that through
`scripts/label_frames.py` works but is a terminal session on one machine; doing it
here makes it a staff task, reviewable and resumable.

Auth tiers, and why they differ across this one router:

  * READS are `ViewerOrAbove`, which is strictly less than a viewer already has:
    GET /clusters/{id} is ViewerOrAbove and returns frames with their scores, behind
    a ViewerOrAbove image route. Gating the queue higher would mean a viewer could
    see a frame through the cluster panel but not through the review list.
  * WRITES are `StaffOrAboveLive` — the tier that re-reads the role from org_member
    rather than trusting the token's login-time snapshot. This is ground truth that
    feeds model training and the promotion gate, so a revoked reviewer must stop
    writing labels *now*, not within the access token's TTL. One extra query next to
    a human staring at a JPEG is free.
  * UNSUBMIT is `AdminOnly`: it retracts somebody else's attestation. Non-destructive
    to the boxes themselves, and rare.

NOT org-scoped, deliberately. asset_frame has no org_id — only asset_cluster got one
(migrations/009), and 009 explicitly declined to invent ownership it could not
justify. Frames are device-scoped and 5,588 of 5,615 came from one phone. The fix
path when devices are enrolled is asset_frame.org_id derived from a device->org
table; until then this is a known gap, not an oversight.

Like the cluster and tile routes, these do NOT take the Accept-Version dependency:
the dashboard ships with the server and is not the versioned mobile client.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Path, Query

from app.dependencies import AdminOnly, DbPool, StaffOrAboveLive, ViewerOrAbove
from app.detection.classes import (
    PRIMARY_CLASS_ID,
    REGION_CLASSES,
    ROAD_SURFACE_CLASSES,
    THIN_ASPECT_RATIO,
    is_thin,
)
from app.models.clusters import DetectionBox
from app.models.review import (
    BoxesRequest,
    BoxesResponse,
    HumanBox,
    ReviewFrameItem,
    ReviewQueueCounts,
    ReviewQueueResponse,
    SubmitRequest,
    SubmitResponse,
    UnsubmitResponse,
    VerdictRequest,
    VerdictResponse,
)
from app.services.detection_boxes import parse_detection_boxes, parse_vlm_verdict
from app.services.review_service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    BoxValidationError,
    finalize_boxes,
    load_queue,
    new_seed,
    parse_human_boxes,
    save_boxes,
    save_verdict,
    unfinalize_boxes,
    validate_boxes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/review", tags=["review"])


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


@router.get("/frames", response_model=ReviewQueueResponse)
async def get_review_queue(
    pool: DbPool,
    _user: ViewerOrAbove,
    mode: str = Query(default="verdict", pattern="^(verdict|box)$"),
    order: str = Query(
        default="score",
        pattern="^(score|blind)$",
        description="score: highest server_probability first. blind: deterministic "
        "shuffle with scores and boxes withheld. 'stratified' remains CLI-only.",
    ),
    review: bool = Query(
        default=False,
        description="Check-my-work: queue ONLY finished frames. Not an "
        "include-everything switch — mixing the two means paging past completed work.",
    ),
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    max_score: float | None = Query(default=None, ge=0.0, le=1.0),
    include_model_boxes: bool = Query(
        default=False,
        description="Opt in to the detector's own boxes. Ignored in blind mode.",
    ),
    seed: int | None = Query(default=None, description="Reproduce a blind ordering."),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> ReviewQueueResponse:
    """One page of frames needing review, with the counts a progress readout needs.

    No cursor: the queue shrinks as it is labelled, so paging over a moving predicate
    would be a false promise. Work through a page, then request the next.
    """
    if min_score is not None and max_score is not None and min_score >= max_score:
        raise HTTPException(status_code=400, detail="min_score must be below max_score.")

    blind = order == "blind"
    resolved_seed = seed if seed is not None else new_seed()

    rows, counts = await load_queue(
        pool,
        mode=mode,
        order=order,
        review=review,
        min_score=min_score,
        max_score=max_score,
        limit=limit,
        seed=resolved_seed,
    )

    items = []
    for r in rows:
        # In blind mode the scores and every box are withheld SERVER-SIDE. Sending
        # them and hiding them in the UI is not blindness; it is one devtools panel
        # away. Anchoring is a measured cause of bad labels, not a style preference.
        show_model = include_model_boxes and not blind
        items.append(
            ReviewFrameItem(
                client_id=r["client_id"],
                ts=_iso(r["ts_utc"]),
                image_url=f"/api/v1/frames/{r['client_id']}/image",
                device_probability=None if blind else r["device_probability"],
                server_probability=None if blind else r["server_probability"],
                label=r["label"],
                note=r["note"],
                labeled_by=r["labeled_by"],
                labeled_at=_iso(r["labeled_at"]),
                boxed_at=_iso(r["boxed_at"]),
                boxes_drafted_at=_iso(r["boxes_drafted_at"]),
                # The reviewer's own prior work is never withheld: this is what makes
                # an interrupted box pass re-adoptable after a reload.
                human_boxes=[HumanBox(**b) for b in parse_human_boxes(r["human_boxes"])],
                server_boxes=(
                    [DetectionBox(**b) for b in parse_detection_boxes(r["server_detections"])]
                    if show_model
                    else []
                ),
                device_boxes=(
                    [DetectionBox(**b) for b in parse_detection_boxes(r["device_detections"])]
                    if show_model
                    else []
                ),
                vlm_verdict=(
                    parse_vlm_verdict(r["server_detections"]) if show_model else None
                ),
            )
        )

    return ReviewQueueResponse(
        items=items,
        counts=ReviewQueueCounts(**counts),
        mode=mode,
        order=order,
        review=review,
        blind=blind,
        seed=resolved_seed,
        classes=list(ROAD_SURFACE_CLASSES),
        primary_class_id=PRIMARY_CLASS_ID,
        region_classes=sorted(REGION_CLASSES),
        thin_aspect_ratio=THIN_ASPECT_RATIO,
        generated_at=datetime.now(UTC).isoformat(),
    )


@router.post("/frames/{client_id}/verdict", response_model=VerdictResponse)
async def post_verdict(
    pool: DbPool,
    staff: StaffOrAboveLive,
    body: VerdictRequest,
    client_id: str = Path(..., min_length=1, max_length=64),
) -> VerdictResponse:
    """Record what a human decided this frame shows.

    labeled_by comes from the token, never the body: accepting it from the client
    would let a reviewer attribute a verdict to a colleague. Every write also appends
    to frame_label_history, because frame_label holds one row per frame and its
    upsert is otherwise silent last-write-wins on training ground truth.
    """
    if body.label not in (1, 0, -1):
        raise HTTPException(status_code=400, detail="label must be 1, 0 or -1.")

    ok = await save_verdict(
        pool,
        client_id=client_id,
        label=body.label,
        note=body.note,
        labeled_by=staff.user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No such frame.")

    return VerdictResponse(
        client_id=client_id,
        label=body.label,
        note=(body.note or "").strip() or None,
        labeled_by=staff.user_id,
        labeled_at=datetime.now(UTC).isoformat(),
    )


@router.post("/frames/{client_id}/boxes", response_model=BoxesResponse)
async def post_boxes(
    pool: DbPool,
    staff: StaffOrAboveLive,
    body: BoxesRequest,
    client_id: str = Path(..., min_length=1, max_length=64),
) -> BoxesResponse:
    """Save this frame's boxes as a DRAFT. Submitting is a separate call.

    Saving writes immediately so a crash or a closed tab costs nothing, but boxed_at
    stays NULL — the frame remains editable and invisible to the exporter until it is
    signed off. Saving ZERO boxes is a real answer ("reviewed, genuinely clean") and
    is recorded via boxes_drafted_at, which is what lets an empty draft survive a
    reload; the CLI's in-process draft set cannot.
    """
    try:
        boxes = validate_boxes([b.model_dump() for b in body.boxes], len(ROAD_SURFACE_CLASSES))
    except BoxValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    ok = await save_boxes(pool, client_id=client_id, boxes=boxes, labeled_by=staff.user_id)
    if not ok:
        # 409, not 404: the frame exists, the caller has simply not met the
        # precondition. Drawing a box before deciding what the frame IS anchors the
        # verdict to the box, which is the anchoring this whole surface avoids.
        raise HTTPException(
            status_code=409,
            detail="This frame has no verdict yet. Judge it before boxing it.",
        )

    # A warning, never a refusal: a thin crack box is usually a mistake (box regions,
    # not lines -- a sliver is mostly undamaged asphalt) but the operator may mean it.
    thin = [
        ROAD_SURFACE_CLASSES[b["class_id"]]
        for b in boxes
        if ROAD_SURFACE_CLASSES[b["class_id"]] in REGION_CLASSES and is_thin(b["w"], b["h"])
    ]

    return BoxesResponse(
        client_id=client_id,
        boxes=[HumanBox(**b) for b in boxes],
        boxed_at=None,
        thin_warnings=sorted(set(thin)),
    )


@router.post("/frames/boxes/submit", response_model=SubmitResponse)
async def post_submit(
    pool: DbPool,
    staff: StaffOrAboveLive,
    body: SubmitRequest,
) -> SubmitResponse:
    """Sign off a batch of drafts — the only thing that writes boxed_at.

    Until this runs, every save is revisable and going back to fix an earlier frame
    costs nothing. After it, the exporter may ship the frame as training data.
    """
    logger.info("review: %s submitting %d frame(s)", staff.user_id, len(body.client_ids))
    return SubmitResponse(**await finalize_boxes(pool, client_ids=body.client_ids))


@router.post("/frames/boxes/unsubmit", response_model=UnsubmitResponse)
async def post_unsubmit(
    pool: DbPool,
    staff: AdminOnly,
    body: SubmitRequest,
) -> UnsubmitResponse:
    """Retract a sign-off, returning frames to the queue with their boxes intact.

    Admin-only: this retracts an attestation that may be somebody else's.
    """
    logger.info("review: %s unsubmitting %d frame(s)", staff.user_id, len(body.client_ids))
    return UnsubmitResponse(cleared=await unfinalize_boxes(pool, client_ids=body.client_ids))
