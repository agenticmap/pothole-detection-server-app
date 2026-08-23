-- ============================================================================
-- Pothole Detection Platform — per-municipality scoping of cluster writes
-- ============================================================================
-- Additive, idempotent. Closes the authorization gap documented in
-- app/routes/clusters.py and deferred by 005_auth.sql: asset_cluster carried no
-- org_id, so ANY staff member of ANY org could mark ANY city's cluster repaired.
-- Reads were already global, which was tolerable; the audited write endpoint
-- (POST /api/v1/clusters/{id}/repair) is what made it matter.
--
-- Deliberately NOT backfilled. There is no municipal boundary table to assign
-- existing rows by geography, and picking a default org would assert ownership
-- that is not real the moment a second municipality exists. NULL therefore means
-- "unowned": visible to all staff, repairable by an 'admin' only. Enforcement
-- lives in app/services/repair_service.py, not in RLS -- the API is the only
-- writer, and an RLS policy would need a per-request SET LOCAL to see the
-- caller's org through the shared asyncpg pool.
-- ============================================================================

ALTER TABLE asset_cluster
    ADD COLUMN IF NOT EXISTS org_id TEXT REFERENCES org(org_id);

-- Partial: the lookups that matter are "this org's clusters", and the NULL rows
-- are the unowned backlog rather than something to scan by org.
CREATE INDEX IF NOT EXISTS idx_asset_cluster_org
    ON asset_cluster (org_id)
    WHERE org_id IS NOT NULL;

COMMENT ON COLUMN asset_cluster.org_id IS
    'Owning municipality, or NULL for unowned (admin-only writes). See migrations/009.';
