"""The frame review queue and its ground-truth writes.

This is the server side of the operator console's labelling surface. It exists to
replace grinding through `scripts/label_frames.py` in a terminal, and it writes the
same two tables (`frame_label`, `frame_box`) by the same rules -- the queue predicate
is shared with the CLI through `app/services/label_queue.py`, and `tests/test_review_db.py`
pins the SQL below against that module's `wants_frame` so the two cannot drift.

Three things here are deliberate and easy to "fix" wrongly:

**No database-name guard.** `scripts/label_frames.py` refuses `pothole_test`/`pothole_ci`
and `scripts/seed_demo.py` refuses everything else; they point opposite ways because
they guard opposite mistakes (destroying real labels vs polluting real data). This
module carries neither. A guard like the CLI's would make these endpoints untestable,
since `tests/conftest.py` *requires* a test database; a guard like the seed's would
refuse to run in production, which is the point of the endpoint. The CLI needs a DSN
check because a CLI has no identity -- nothing distinguishes a careless invocation
from a deliberate one. The API substitutes something strictly better: authentication,
a role floor, `labeled_by` taken from the token, and an append-only history.

**`labeled_by` is never client-supplied.** Accepting it from the body would let a
reviewer attribute a verdict to a colleague.

**Blind mode omits scores server-side rather than hiding them client-side.** In the
CLI blindness is a rendering property (the `b` key). In a browser that is not
blindness -- the number is in the JSON. Anchoring the labeller to the model's opinion
is a *measured* cause of bad labels (the v2->v4 recall collapse), so what the reviewer
must not see is not sent.
"""

from __future__ import annotations

import json
import logging
import uuid

import asyncpg

from app.services.label_queue import wants_frame

logger = logging.getLogger(__name__)

# A note is free text, so it gets a ceiling before it reaches the database. Mirrors
# scripts/label_frames.py's _MAX_NOTE; 200 is far more than "manhole" needs.
MAX_NOTE = 200

# Bounds on a page. The CLI loads a fixed working set (--count, default 300) and the
# console does the same: the queue shrinks as you label it, so a cursor over a moving
# predicate would be a false promise. Re-request for the next batch.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Cap on a single submit/unsubmit call, so one request cannot retract a whole corpus.
MAX_BATCH = 200


# -- Box validation -----------------------------------------------------------


class BoxValidationError(ValueError):
    """A box the operator drew cannot be stored. The message is shown to them."""


def validate_boxes(raw: object, class_count: int) -> list[dict]:
    """Check a submitted box set against frame_box's constraints, before SQL does.

    Same rules as scripts/label_frames.py's box POST handler, extracted so both
    clients enforce them identically. These are not belt-and-braces over the CHECK
    constraints in migrations/013 -- they exist so a bad box is a 400 with a reason
    rather than a 500 from a constraint violation.

    The 1.0001 slack on the right and bottom edges is 013's, and it is load-bearing:
    it absorbs float rounding on the browser's pixel -> fraction conversion. A hard 1.0
    would reject a box drawn flush to the frame edge, which is exactly where a pothole
    at the shoulder sits.
    """
    if not isinstance(raw, list):
        raise BoxValidationError("boxes must be a list")
    boxes: list[dict] = []
    for b in raw:
        if not isinstance(b, dict):
            raise BoxValidationError("malformed box")
        try:
            class_id = int(b["class_id"])
            x, y, w, h = (float(b[k]) for k in ("x", "y", "w", "h"))
        except (TypeError, ValueError, KeyError) as e:
            raise BoxValidationError("malformed box") from e
        if not 0 <= class_id < class_count:
            raise BoxValidationError("unknown class_id")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise BoxValidationError("box origin outside the frame")
        if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            raise BoxValidationError("box has no area")
        if x + w > 1.0001 or y + h > 1.0001:
            raise BoxValidationError("box extends past the frame")
        boxes.append({"class_id": class_id, "x": x, "y": y, "w": w, "h": h})
    return boxes


# -- The queue ----------------------------------------------------------------

# The predicate is pushed into SQL rather than filtered in Python as the CLI does.
# label_frames' select has no WHERE and no LIMIT -- it pulls every frame into the
# process and filters there, which is fine for a one-person tool over 5,615 rows and
# not acceptable on an HTTP endpoint. That makes this a SECOND implementation of
# label_queue.wants_frame, so tests/test_review_db.py asserts the two select identical
# id sets over a population covering the whole truth table, and _verify_page below
# re-checks every returned row against the Python original.
_PREDICATES = {
    ("verdict", False): "l.label IS NULL",
    ("verdict", True): "l.label IS NOT NULL",
    # Box mode never queues an unjudged frame in either direction: drawing a box
    # before deciding what the frame IS anchors the verdict to the box.
    ("box", False): "l.label IS NOT NULL AND l.boxed_at IS NULL",
    ("box", True): "l.label IS NOT NULL AND l.boxed_at IS NOT NULL",
}

_ORDERS = {
    # Highest score first. Unscored frames are EXCLUDED, not ranked last: a NULL means
    # the backfill has not seen the frame, and sorting it as 0.0 buries real frames
    # under unscored ones. Matches label_queue.rank_by_score.
    "score": "f.server_probability DESC, f.client_id",
    # Deterministic, seedable, and independent of the score -- so a blind pass is
    # reproducible without the ordering itself leaking the model's opinion.
    "blind": "md5(f.client_id || $SEED$), f.client_id",
}

_QUEUE_SQL = """
SELECT f.client_id,
       f.ts_utc,
       f.device_probability,
       f.server_probability,
       f.server_detections,
       f.device_detections,
       l.label,
       l.note,
       l.labeled_by,
       l.labeled_at,
       l.boxed_at,
       l.boxes_drafted_at,
       COALESCE((
           SELECT json_agg(json_build_object(
                      'class_id', b.class_id, 'x', b.x, 'y', b.y, 'w', b.w, 'h', b.h)
                  ORDER BY b.id)
           FROM frame_box b WHERE b.frame_client_id = f.client_id
       ), '[]'::json)::text AS human_boxes
FROM asset_frame f
LEFT JOIN frame_label l ON l.frame_client_id = f.client_id
WHERE {predicate}
  {band}
  {scored}
ORDER BY {order}
LIMIT {limit_param}
"""

_COUNT_SQL = """
SELECT
    count(*) FILTER (WHERE {outstanding}) AS outstanding,
    count(*) FILTER (WHERE {done})        AS done,
    count(*)                              AS in_band
FROM asset_frame f
LEFT JOIN frame_label l ON l.frame_client_id = f.client_id
WHERE TRUE {band} {scored}
"""


def _band_clause(min_score: float | None, max_score: float | None) -> tuple[str, list]:
    """Half-open [min, max) so adjacent bands from the phase-2.9 table cannot double-count."""
    parts: list[str] = []
    params: list = []
    if min_score is not None:
        params.append(min_score)
        parts.append(f"AND f.server_probability >= ${len(params)}")
    if max_score is not None:
        params.append(max_score)
        parts.append(f"AND f.server_probability < ${len(params)}")
    return " ".join(parts), params


async def load_queue(
    pool: asyncpg.Pool,
    *,
    mode: str,
    order: str,
    review: bool,
    min_score: float | None,
    max_score: float | None,
    limit: int,
    seed: int,
) -> tuple[list[asyncpg.Record], dict]:
    """Return one page of the queue plus the counts a progress readout needs."""
    predicate = _PREDICATES[(mode, review)]
    band, band_params = _band_clause(min_score, max_score)
    # A score-ordered or band-filtered pass is only meaningful over scored frames.
    scored = (
        "AND f.server_probability IS NOT NULL"
        if order == "score" or min_score is not None or max_score is not None
        else ""
    )

    params = list(band_params)
    if order == "blind":
        params.append(str(seed))
        order_sql = _ORDERS["blind"].replace("$SEED$", f"${len(params)}")
    else:
        order_sql = _ORDERS[order]

    sql = _QUEUE_SQL.format(
        predicate=predicate,
        band=band,
        scored=scored,
        order=order_sql,
        limit_param=f"${len(params) + 1}",
    )
    count_sql = _COUNT_SQL.format(
        outstanding=_PREDICATES[(mode, False)],
        done=_PREDICATES[(mode, True)],
        band=band,
        scored=scored,
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params, limit)
        counts = await conn.fetchrow(count_sql, *band_params)

    return _verify_page(rows, mode=mode, review=review), dict(counts)


def _verify_page(rows: list[asyncpg.Record], *, mode: str, review: bool) -> list[asyncpg.Record]:
    """Re-check the SQL predicate against the Python one it duplicates.

    Costs microseconds on a page of 50 and turns a silent divergence -- the failure
    mode of having two implementations of one rule -- into a log line plus a dropped
    row rather than a frame the operator should never have been shown.
    """
    kept = []
    for r in rows:
        if wants_frame(r, box=(mode == "box"), review=review):
            kept.append(r)
        else:
            logger.warning(
                "review queue: SQL returned %s which wants_frame rejects "
                "(mode=%s review=%s label=%r boxed_at=%r); dropping it. "
                "The two predicates have drifted -- see _PREDICATES.",
                r["client_id"], mode, review, r["label"], r["boxed_at"],
            )
    return kept


# -- Writes -------------------------------------------------------------------

_UPSERT_VERDICT_SQL = """
INSERT INTO frame_label (frame_client_id, label, labeled_by, note)
VALUES ($1, $2, $3, $4)
ON CONFLICT (frame_client_id) DO UPDATE
SET label = EXCLUDED.label, labeled_by = EXCLUDED.labeled_by,
    labeled_at = now(), note = EXCLUDED.note
"""

# One INSERT per verdict. frame_label's PK is one row per frame and its write is an
# upsert, so a second annotator silently overwrites the first. This does not prevent
# that -- it makes it recoverable, and makes inter-annotator agreement measurable
# retroactively. See migrations/017.
_HISTORY_SQL = """
INSERT INTO frame_label_history (frame_client_id, label, note, labeled_by, source)
VALUES ($1, $2, $3, $4, 'api')
"""

_FRAME_EXISTS_SQL = "SELECT 1 FROM asset_frame WHERE client_id = $1"

_JUDGED_ONE_SQL = """
SELECT 1 FROM frame_label WHERE frame_client_id = $1 AND label IS NOT NULL
"""

_DELETE_BOXES_SQL = "DELETE FROM frame_box WHERE frame_client_id = $1"

_INSERT_BOX_SQL = """
INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

# Set even when zero boxes were drawn -- that is what makes "I looked, there is
# nothing here" survive a reload, which the CLI's in-process draft set cannot.
_MARK_DRAFTED_SQL = """
UPDATE frame_label SET boxes_drafted_at = now() WHERE frame_client_id = $1
"""

# `boxes_drafted_at IS NOT NULL` is a data-integrity guard, not a nicety.
#
# scripts/export_labeled_frames.py keys on boxed_at ALONE and says so in its own
# docstring: "REVIEWED IS NOT THE SAME AS EMPTY. A frame with boxed_at IS NULL has
# never been opened, so its lack of boxes means nothing; exporting it as background
# is precisely the mistake above." So signing off a frame nobody ever drew on turns
# it into an empty .txt -- a YOLO BACKGROUND image asserting "genuinely clean road".
# That is the exact mechanism that took recall 0.708 -> 0.215 across v2/v3/v4.
#
# The client sends the id list, so without this the guard would live only in a
# browser. A ground-truth write that feeds the promotion gate does not get to be
# guarded client-side. Frames filtered out here come back as `skipped_undrafted`.
_MARK_BOXED_SQL = """
UPDATE frame_label SET boxed_at = now()
WHERE frame_client_id = ANY($1::text[])
  AND boxed_at IS NULL
  AND boxes_drafted_at IS NOT NULL
RETURNING frame_client_id
"""

_UNMARK_BOXED_SQL = """
UPDATE frame_label SET boxed_at = NULL
WHERE frame_client_id = ANY($1::text[]) AND boxed_at IS NOT NULL
RETURNING frame_client_id
"""

_JUDGED_MANY_SQL = """
SELECT frame_client_id, boxes_drafted_at, boxed_at FROM frame_label
WHERE frame_client_id = ANY($1::text[]) AND label IS NOT NULL
"""


async def save_verdict(
    pool: asyncpg.Pool, *, client_id: str, label: int, note: str | None, labeled_by: str
) -> bool:
    """Record a verdict. Returns False if the frame does not exist."""
    clean = (note or "").strip()[:MAX_NOTE] or None
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval(_FRAME_EXISTS_SQL, client_id):
            return False
        await conn.execute(_UPSERT_VERDICT_SQL, client_id, label, labeled_by, clean)
        await conn.execute(_HISTORY_SQL, client_id, label, clean, labeled_by)
    return True


async def save_boxes(
    pool: asyncpg.Pool, *, client_id: str, boxes: list[dict], labeled_by: str
) -> bool:
    """Replace this frame's boxes and mark it drafted. Returns False if unjudged.

    Replace-all, not upsert: a frame holds many boxes and the surrogate key is
    meaningless to the client, so "these are the boxes now" is the only sane contract.
    Delete-then-insert inside one transaction keeps a half-saved frame from ever being
    visible. Saving leaves the frame a DRAFT -- boxed_at stays NULL, so nothing
    downstream can consume it until it is submitted.
    """
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval(_JUDGED_ONE_SQL, client_id):
            return False
        await conn.execute(_DELETE_BOXES_SQL, client_id)
        for b in boxes:
            await conn.execute(
                _INSERT_BOX_SQL,
                client_id, b["class_id"], b["x"], b["y"], b["w"], b["h"], labeled_by,
            )
        await conn.execute(_MARK_DRAFTED_SQL, client_id)
    return True


async def finalize_boxes(pool: asyncpg.Pool, *, client_ids: list[str]) -> dict:
    """Sign off drafts. Reports every frame it refused, rather than silently no-opping.

    Two refusals, both reported so the operator learns why 3 of their 50 did not
    finalize rather than discovering it in a training run:

      * `skipped_unjudged` -- no verdict at all. The marking UPDATE runs on
        frame_label, so such a frame has no row to update and would vanish silently.
      * `skipped_undrafted` -- judged, but boxes were never saved. See _MARK_BOXED_SQL
        for why signing one of these off would poison the training set.
    """
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(_JUDGED_MANY_SQL, client_ids)
        judged = {r["frame_client_id"] for r in rows}
        already = {r["frame_client_id"] for r in rows if r["boxed_at"] is not None}
        undrafted = {
            r["frame_client_id"]
            for r in rows
            if r["boxes_drafted_at"] is None and r["boxed_at"] is None
        }
        finalized = [r["frame_client_id"] for r in await conn.fetch(_MARK_BOXED_SQL, client_ids)]
    return {
        "finalized": len(finalized),
        "already_finalized": len(already),
        "skipped_unjudged": sorted(set(client_ids) - judged),
        "skipped_undrafted": sorted(undrafted),
    }


async def unfinalize_boxes(pool: asyncpg.Pool, *, client_ids: list[str]) -> int:
    """Return signed-off frames to the queue. Boxes are deliberately NOT touched --
    the marker says "a human signed this off", and clearing it keeps their work."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_UNMARK_BOXED_SQL, client_ids)
    return len(rows)


def parse_human_boxes(raw: str | None) -> list[dict]:
    """The queue's human_boxes column arrives as JSON text."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return items if isinstance(items, list) else []


def new_seed() -> int:
    """A blind pass needs a stable seed the client can carry in its URL."""
    return uuid.uuid4().int % 1_000_000
