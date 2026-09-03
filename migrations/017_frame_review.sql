-- ============================================================================
-- Pothole Detection Platform — frame review from the operator console
-- ============================================================================
-- Additive, idempotent. Phase 2.7d (review surface).
--
-- Why this migration exists. Labelling has been a one-person terminal tool
-- (scripts/label_frames.py) writing frame_label and frame_box. Moving it into the
-- staff console changes two things the schema cannot currently express, and both
-- of them silently corrupt ground truth that feeds scripts/promote_model.py.
--
-- 1. THE ZERO-BOX DRAFT. 013's boxed_at is the "a human reviewed this" marker, and
--    it is deliberately not "does the frame have boxes" -- a frame reviewed and
--    found genuinely clean has zero boxes and IS finished, while a frame nobody
--    opened also has zero boxes and is NOT. Only the second may be shipped as a
--    YOLO background image. But the CLI records a draft by writing the boxes
--    themselves, so a draft with zero boxes leaves no trace at all and cannot be
--    re-adopted after a crash (label_frames.py says so in its own docstring). Over
--    HTTP that hole is worse: a client that POSTed an empty box set and a client
--    that never opened the frame are indistinguishable at submit time.
--    boxes_drafted_at closes it. Set by every box save INCLUDING the empty one;
--    boxed_at still gates the exporter, so nothing downstream changes.
--
-- 2. CONCURRENT ANNOTATORS. frame_label's PK is one row per frame and its write is
--    an upsert, so with one CLI and one person "last write wins" was invisible.
--    A multi-user console makes it reachable, and the research record already names
--    the limitation: "Single annotator, single pass. frame_label's primary key is
--    one row per frame, so the schema cannot currently express disagreement."
--    frame_label_history makes an overwritten verdict recoverable and inter-annotator
--    agreement measurable retroactively, for one INSERT per label. It does not
--    prevent the overwrite -- that is a 409 in the route, if it is ever wanted.
--
-- Two FK asymmetries, both copied from 008_repair_log deliberately:
--   * labeled_by has NO foreign key to staff_user, so the record of who judged a
--     frame outlives the deletion of their account.
--   * frame_client_id has NO cascade, for the same reason.
-- ============================================================================

-- ── 1. The draft marker ──────────────────────────────────────────────────────

ALTER TABLE frame_label ADD COLUMN IF NOT EXISTS boxes_drafted_at TIMESTAMPTZ;

COMMENT ON COLUMN frame_label.boxes_drafted_at IS
    'When boxes were last SAVED for this frame, including a deliberate empty set. '
    'Distinct from boxed_at, which means a human signed the frame off. '
    'boxes_drafted_at IS NOT NULL AND boxed_at IS NULL == an unsubmitted draft.';

-- ── 2. Verdict history ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS frame_label_history (
    history_id      BIGSERIAL PRIMARY KEY,
    frame_client_id TEXT        NOT NULL,
    label           SMALLINT    NOT NULL CHECK (label IN (1, 0, -1)),
    note            TEXT,
    -- Free text from the CLI (--by "sean"), a usr_<uuid> from the console. Two
    -- namespaces in one column, on purpose: any query grouping by annotator needs
    -- to know, and rewriting the CLI's 340 existing rows would falsify them.
    labeled_by      TEXT        NOT NULL,
    source          TEXT        NOT NULL CHECK (source IN ('cli', 'api')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_frame_label_history_frame
    ON frame_label_history (frame_client_id, created_at DESC);

-- ── 3. Indexes the review queue needs ────────────────────────────────────────
-- At 5,615 rows none of this matters. It is written now because the queue's two
-- orderings are score-desc and "outstanding work", and both become sequential
-- scans the moment the corpus is a season of driving rather than two weeks of it.

CREATE INDEX IF NOT EXISTS idx_asset_frame_server_probability
    ON asset_frame (server_probability DESC, client_id)
    WHERE server_probability IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_frame_label_unboxed
    ON frame_label (frame_client_id)
    WHERE boxed_at IS NULL;
