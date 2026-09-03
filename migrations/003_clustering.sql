-- ============================================================================
-- Pothole Detection Platform — Crowd Clustering (Phase 2.2)
-- ============================================================================
-- Additive, idempotent migration. Does NOT modify 001 or 002. Applied after
-- them by the sorted-glob loader in app/database.py::run_migrations.
--
-- The destination tables (asset_cluster, observation_cluster_link) already
-- exist from 001 — they were scaffolded empty for this phase. This migration
-- only adds the indexes and the audit table that the clustering job needs.
--
-- See docs/phases/phase-2.2-clustering-plan.md for the original design -- but note
-- that TWO of its decisions have since been replaced: ST_ClusterDBSCAN gave way to a
-- two-stage Python assignment (it cannot buffer each event by its own GPS accuracy,
-- and its chaining produced a 124 m "single pothole"), and cluster_min_points is now
-- 1 rather than 3. docs/architecture/from-reading-to-defect.md describes what runs.
-- ============================================================================

-- ============================================================================
-- INDEXES for the clustering job
-- ============================================================================

-- The job rebuilds links per touched cluster (DELETE ... WHERE member_id = ANY)
-- and looks members up by id; index the lookup column.
CREATE INDEX IF NOT EXISTS idx_observation_cluster_link_member
    ON observation_cluster_link (member_id);

-- Candidate scan: pothole-classed, scored observations. Partial index keeps it
-- small (only the rows the member-selection CTE cares about).
CREATE INDEX IF NOT EXISTS idx_asset_observation_pothole
    ON asset_observation (received_at)
    WHERE sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE;

-- ============================================================================
-- CLUSTER RUN (audit trail for clustering job executions)
-- ============================================================================
-- Mirrors fusion_run: one row per clustering tick, for reproducibility and
-- ops visibility (how many members went in, how many clusters came out).
-- ============================================================================

CREATE TABLE IF NOT EXISTS cluster_run (
    run_id              TEXT PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    inputs_count        INT,           -- member observations considered
    outputs_count       INT,           -- clusters upserted (created + updated)
    params_jsonb        JSONB          -- {eps_m, min_points, window_days, ...}
);
