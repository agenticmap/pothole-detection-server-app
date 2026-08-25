-- ============================================================================
-- Pothole Detection Platform — human ground truth on uploaded frames
-- ============================================================================
-- Additive, idempotent. Phase 2.7.
--
-- Why this table exists. Turning on server-side detection produces a
-- server_probability for every frame, but nothing in the schema could say whether
-- that number is RIGHT. The only ground-truth slot that existed was
-- asset_observation.visual_confirmed, which is (a) NULL on all 2728 rows, (b) at
-- the wrong grain -- it describes a sensor observation, not the image a detector
-- scored, and the two are only linked through fusion_pair. mAP on the training
-- archive measures a different distribution entirely (landscape GoPro/Japan road
-- imagery, not 480x640 portrait windshield frames through Toronto rain).
--
-- Frame-level binary labels, not boxes. Everything downstream consumes a SCALAR:
-- app/fusion/service.py takes COALESCE(server_probability, device_probability) and
-- never looks at server_detections. So the question that decides whether detection
-- is working is "does this frame contain a pothole", and answering it for a few
-- hundred frames is minutes of work rather than hours of box-drawing. If per-box
-- localization ever needs measuring, that is a separate, additive table.
--
-- label semantics match roadmap.md 3.1's planned event_label (1 / 0 / -1) so the
-- Phase 3 flywheel's POST /api/v1/labels can adopt them without a translation.
-- -1 ("unsure") is kept rather than discarded: an unreadable night frame is a real
-- and interesting category, and silently dropping it would bias the measured
-- precision upward.
--
-- One row per frame (frame_client_id is the PK), so re-labelling is an upsert and
-- a frame cannot be double-counted in a metric.
-- ============================================================================

CREATE TABLE IF NOT EXISTS frame_label (
    frame_client_id TEXT PRIMARY KEY REFERENCES asset_frame(client_id) ON DELETE CASCADE,
    label           SMALLINT NOT NULL CHECK (label IN (1, 0, -1)),
    labeled_by      TEXT NOT NULL,
    labeled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    note            TEXT
);

COMMENT ON TABLE frame_label IS
    'Human ground truth for detector evaluation. 1 = contains a pothole, 0 = does '
    'not, -1 = cannot tell. Frame-level, because the pipeline consumes a scalar '
    'probability (see app/fusion/service.py COALESCE), not boxes.';

-- The evaluation query is "every labelled frame, joined to its scores", and the
-- labelled set is a small slice of asset_frame. Indexing the label lets that
-- aggregate by class without a scan once the set grows.
CREATE INDEX IF NOT EXISTS idx_frame_label_label ON frame_label (label);
