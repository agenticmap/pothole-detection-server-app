-- ============================================================================
-- Pothole Detection Platform — human box annotations on uploaded frames
-- ============================================================================
-- Additive, idempotent. Phase 2.7b.
--
-- Why this table exists. 010_frame_label gave every frame a verdict, and that was
-- the right grain for measuring a scalar probability. It is the wrong grain for
-- TRAINING. A detector cannot learn a positive without coordinates, so the only
-- rows scripts/export_negatives.py could consume were the label = 0 ones, as
-- background images -- and those are HARD negatives (manholes, tar seals, grates:
-- dark, roughly pothole-shaped, on road surface in the wheel path). With a single
-- class the model's only way to explain a manhole is "background", which it
-- achieves by suppressing dark-irregular-shape-on-asphalt. Real potholes are dark
-- irregular shapes on asphalt. Measured across three models, each given more hand
-- labels than the last: recall 0.708 -> 0.431 -> 0.354. Labelling was making the
-- detector monotonically worse. See docs/phases/phase-2.7b-road-surface-classes.md.
--
-- 010's own comment anticipated this table: "If per-box localization ever needs
-- measuring, that is a separate, additive table."
--
-- CORNER-ORIGIN, NORMALIZED 0..1 -- not YOLO's centre-origin. This is the
-- convention device_detections and server_detections already emit (see
-- app/detection/onnx_v1.py's module docstring). YOLO's .txt format wants
-- centre-origin, so that conversion happens exactly once, at export, with a
-- round-trip test. Two box conventions in one database is precisely the class of
-- bug onnx_v1._check_layout exists to catch: plausible-looking garbage rather than
-- a loud failure.
--
-- Many rows per frame (a frame can hold a pothole AND a manhole), so the PK is a
-- surrogate and re-annotating is delete-then-insert in one transaction rather than
-- an upsert. That is why the reviewed marker cannot live in this table -- see
-- frame_label.boxed_at below.
--
-- class_id is deliberately unconstrained beyond >= 0. The class list is
-- configuration (DETECTION_CLASS_NAMES), not schema; pinning the set here would
-- mean a migration every time a class is added, and the decoder already fails
-- loudly on a class-count mismatch.
-- ============================================================================

CREATE TABLE IF NOT EXISTS frame_box (
    id              BIGSERIAL PRIMARY KEY,
    frame_client_id TEXT NOT NULL REFERENCES asset_frame(client_id) ON DELETE CASCADE,
    class_id        SMALLINT NOT NULL CHECK (class_id >= 0),
    x               DOUBLE PRECISION NOT NULL CHECK (x >= 0.0 AND x <= 1.0),
    y               DOUBLE PRECISION NOT NULL CHECK (y >= 0.0 AND y <= 1.0),
    w               DOUBLE PRECISION NOT NULL CHECK (w > 0.0 AND w <= 1.0),
    h               DOUBLE PRECISION NOT NULL CHECK (h > 0.0 AND h <= 1.0),
    labeled_by      TEXT NOT NULL,
    labeled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The 1.0001 slack absorbs float rounding on the browser's pixel -> fraction
    -- conversion. A hard 1.0 would reject a box drawn flush to the frame edge,
    -- which is exactly where a pothole at the shoulder sits.
    CONSTRAINT frame_box_within_frame CHECK (x + w <= 1.0001 AND y + h <= 1.0001)
);

COMMENT ON TABLE frame_box IS
    'Human box annotations for detector training. Corner-origin, normalized 0..1 '
    '-- the same convention device_detections/server_detections emit. Converted to '
    'YOLO centre-origin once, at export. frame_label keeps the frame-level verdict; '
    'this adds localization. Neither replaces the other.';

-- The export query is "every box for these frames", keyed by frame.
CREATE INDEX IF NOT EXISTS idx_frame_box_frame ON frame_box (frame_client_id);
-- Per-class counts are how you check a data-poor class is present at all before
-- spending hours training on it.
CREATE INDEX IF NOT EXISTS idx_frame_box_class ON frame_box (class_id);

-- Reviewed-and-clean is NOT the same as never-reviewed, and the difference decides
-- whether a frame may be exported as a YOLO background image. Absence of rows in
-- frame_box cannot express it, so the marker lives on the frame instead. Exporting
-- an unreviewed frame as background is the exact mistake this phase exists to undo.
ALTER TABLE frame_label ADD COLUMN IF NOT EXISTS boxed_at TIMESTAMPTZ;

COMMENT ON COLUMN frame_label.boxed_at IS
    'When a human last reviewed this frame for boxes. NULL = never reviewed, so it '
    'must not be exported as a background image. Set even when zero boxes were '
    'drawn -- that is the "reviewed, genuinely clean" case.';
