-- ============================================================================
-- Pothole Detection Platform — Server-side Detection Model (Phase 2.3)
-- ============================================================================
-- Additive, idempotent migration. Does NOT modify 001–003. Applied after them
-- by the sorted-glob loader in app/database.py::run_migrations.
--
-- Adds:
--   - asset_frame.detected_at : claimed by the detection worker, INDEPENDENT of
--                               processed_at (which the fusion job owns). Keeping
--                               them separate lets detection + fusion run without
--                               racing for the same flag.
--   - model_disagreement      : device↔server probability divergence log, for the
--                               Phase-3 labeling flywheel.
--
-- The server_probability / server_model_id / server_detections columns already
-- exist (001); the detection worker is what finally populates them.
-- See docs/phase-2.3-detection-plan.md.
-- ============================================================================

-- Detection-completion flag (mirrors asset_frame.processed_at for the fusion job).
ALTER TABLE asset_frame ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ;

-- Undetected frames queue for the detection worker.
CREATE INDEX IF NOT EXISTS idx_asset_frame_undetected
    ON asset_frame (received_at) WHERE detected_at IS NULL;

-- ============================================================================
-- MODEL DISAGREEMENT (device vs. server probability divergence)
-- ============================================================================
-- One row per frame where |device_probability − server_probability| exceeds the
-- configured threshold. Seeds Phase-3 review / labeling.
-- ============================================================================

CREATE TABLE IF NOT EXISTS model_disagreement (
    id                  BIGSERIAL PRIMARY KEY,
    frame_client_id     TEXT NOT NULL REFERENCES asset_frame(client_id) ON DELETE CASCADE,
    device_probability  DOUBLE PRECISION,
    server_probability  DOUBLE PRECISION,
    delta               DOUBLE PRECISION,
    server_model_id     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_model_disagreement_frame
    ON model_disagreement (frame_client_id);
