-- ============================================================================
-- Pothole Detection Platform — Sensor Model + Fusion (Phase 2.1)
-- ============================================================================
-- Additive, idempotent migration. Does NOT modify 001. Applied after 001 by
-- the sorted-glob loader in app/database.py::run_migrations.
--
-- Adds:
--   - sensor_model        : versioned, frozen artifacts of the unsupervised
--                           accelerometer classifier (ported from the 2017
--                           MATLAB GMM/Gaussian-NB research). One active row.
--   - asset_observation.* : per-observation scoring results written by the
--                           fusion job's scoring step.
--   - fusion_pair.severity: per-pair severity captured alongside confidence.
--
-- See docs/phase-2.1-fusion-engine-plan.md for the full design.
-- ============================================================================

-- ============================================================================
-- SENSOR MODEL (versioned classifier artifacts)
-- ============================================================================
-- Each fit produces a new row. `is_active` marks the model the scorer loads.
-- A refit = a new version (rollback by flipping is_active). The fitted sklearn
-- GMM + IsolationForest are joblib-pickled into model_blob; sklearn_version is
-- pinned so unpickling stays compatible. The JSONB columns hold the
-- human-readable / reproducible parameters (also sufficient to re-derive
-- scoring without unpickling, for audit).
-- ============================================================================

CREATE TABLE IF NOT EXISTS sensor_model (
    model_version           TEXT PRIMARY KEY,
    fitted_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_observations          INT NOT NULL,
    k                       INT NOT NULL,
    bic                     DOUBLE PRECISION,
    standardization_jsonb   JSONB NOT NULL,   -- {ratio: {mean, std}, gbar: {mean, std}, ...}
    components_jsonb        JSONB NOT NULL,   -- [{mu, sigma, weight}, ...] per GMM component
    class_map_jsonb         JSONB NOT NULL,   -- {component_index: "pothole"|"crack"|"not", ...}
    severity_calib_jsonb    JSONB,            -- {speed_ref, scale, ...}
    model_blob              BYTEA,            -- joblib-pickled (GMM, IsolationForest)
    sklearn_version         TEXT,
    is_active               BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one active model. Partial unique index enforces it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sensor_model_active
    ON sensor_model (is_active) WHERE is_active;

-- ============================================================================
-- ASSET OBSERVATION — scoring result columns
-- ============================================================================
-- Populated by the fusion job's scoring step. scored_at gates incrementality
-- (only unscored rows are picked up), mirroring asset_frame.processed_at.
-- ============================================================================

ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS sensor_class          TEXT;
ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS sensor_p_pothole      DOUBLE PRECISION;
ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS sensor_severity       DOUBLE PRECISION;
ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS sensor_is_outlier     BOOLEAN;
ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS sensor_model_version  TEXT;
ALTER TABLE asset_observation ADD COLUMN IF NOT EXISTS scored_at             TIMESTAMPTZ;

-- Unscored observations queue for the scoring step.
CREATE INDEX IF NOT EXISTS idx_asset_observation_unscored
    ON asset_observation (received_at) WHERE scored_at IS NULL;

-- ============================================================================
-- FUSION PAIR — severity
-- ============================================================================
-- Per-pair severity captured alongside fused_confidence (engine output).
-- ============================================================================

ALTER TABLE fusion_pair ADD COLUMN IF NOT EXISTS severity DOUBLE PRECISION;
