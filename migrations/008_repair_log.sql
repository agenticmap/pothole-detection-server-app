-- ============================================================================
-- 008: Repair audit trail (Phase 2.5)
-- ============================================================================
-- Additive, idempotent — run_migrations re-executes every .sql on each dev boot.
--
-- Until now asset_cluster.repaired_at had no write path anywhere in app/: the
-- roadmap assumed an admin would set it by hand in Supabase Studio. The operator
-- dashboard makes it a first-class action (POST /api/v1/clusters/{id}/repair),
-- and a municipal repair record needs to say who marked it and when.
--
-- Un-repairing appends a second row rather than deleting the first — mis-clicks
-- happen and the history is the point.
--
-- Two deliberate asymmetries in the foreign keys:
--   * user_id has NO foreign key. An audit row must outlive the account that
--     wrote it; ON DELETE CASCADE from staff_user would erase exactly the
--     history this table exists to keep.
--   * cluster_id keeps its foreign key, but with the default NO ACTION rather
--     than CASCADE, for the same reason. The FK still earns its place by
--     rejecting a typo'd cluster_id at write time.
-- ============================================================================

CREATE TABLE IF NOT EXISTS repair_log (
    repair_id   TEXT PRIMARY KEY,                                  -- 'rpr_<uuid4 hex>'
    cluster_id  TEXT NOT NULL REFERENCES asset_cluster(cluster_id),
    action      TEXT NOT NULL CHECK (action IN ('repaired', 'unrepaired')),
    note        TEXT,
    user_id     TEXT NOT NULL,
    org_id      TEXT,
    repaired_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN repair_log.user_id IS
    'staff_user.user_id of the actor. Intentionally NOT a foreign key so the audit row survives account deletion.';
COMMENT ON COLUMN repair_log.org_id IS
    'Actor org at the time of the action. Recorded so per-municipality RLS can be retrofitted (see 005_auth.sql).';
COMMENT ON COLUMN repair_log.repaired_at IS
    'The repaired_at value written to asset_cluster, so the audit row is self-describing without re-joining the mutable cluster.';

-- History lookup for the detail panel: newest first, per cluster.
CREATE INDEX IF NOT EXISTS idx_repair_log_cluster
    ON repair_log (cluster_id, created_at DESC);
