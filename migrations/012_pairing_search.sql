-- ============================================================================
-- Pothole Detection Platform — pairing search (Phase 2.2d)
-- ============================================================================
-- Additive, idempotent. Supports the lookahead-aware pairing search described in
-- docs/phases/phase-2.2d-pairing-search.md.
--
-- Two columns:
--
-- 1. fusion_pair.match_cost -- the value of the ranking cost that won this pair.
--    Stored so a bad pairing is diagnosable after the fact: the cost mixes a
--    lead-band penalty with a kinematic residual, and without the number there is
--    no way to tell which term decided. Also what scripts/pairing_eval.py reports
--    a distribution over. NULL for pairs written before this migration.
--
-- 2. fusion_pair.is_primary -- whether this frame is the geometrically best view
--    of its observation. One observation can win MANY frames (the pairing is
--    per-frame, so 1842 pairs in pothole_db covered only 472 observations), and
--    _MEMBERS_CTE previously took max(fused_confidence) over all of them. A max
--    over N correlated views of the same pavement cherry-picks the most agreeable
--    one and the bias grows with N -- measured at +0.148 on the visual term over
--    346 multi-frame events. The flag lets the member gate use the best VIEW
--    rather than the best VERDICT, while keeping every pair as an audit trail.
--
-- The partial unique index is the real guarantee. is_primary is a per-observation
-- fact but pairing runs per frame batch, so a later batch can find a better frame
-- for an already-paired observation; the job must demote the old primary in the
-- same transaction. Without the index that invariant is a comment, not a rule.
--
-- No backfill. Existing pairs keep is_primary = false and the member gate falls
-- back to the old max() when an observation has no primary, so this migration
-- changes nothing until frames are re-fused (set asset_frame.processed_at = NULL).
-- ============================================================================

ALTER TABLE fusion_pair
    ADD COLUMN IF NOT EXISTS match_cost DOUBLE PRECISION;

ALTER TABLE fusion_pair
    ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN fusion_pair.match_cost IS
    'Ranking cost that selected this pair: lead-band penalty + kinematic residual '
    '+ forward-frame penalty. Lower is better. NULL for pairs written before '
    'Phase 2.2d, or when FUSION_PAIRING_COST_ENABLED=false.';

COMMENT ON COLUMN fusion_pair.is_primary IS
    'TRUE for the geometrically best frame of this observation (lowest match_cost). '
    'The member gate reads the primary rather than max(fused_confidence) over all '
    'frames, which would cherry-pick the most agreeable of N correlated views.';

-- At most one primary frame per observation.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fusion_pair_primary
    ON fusion_pair (event_client_id)
    WHERE is_primary;

-- The member gate joins fusion_pair by event_client_id and filters on is_primary;
-- the unique index above already covers that lookup. This one serves the reverse
-- direction: demoting the previous primary needs every pair of a given frame.
CREATE INDEX IF NOT EXISTS idx_fusion_pair_frame
    ON fusion_pair (frame_client_id);
