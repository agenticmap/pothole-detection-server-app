-- ============================================================================
-- Pothole Detection Platform — spatiotemporal crowd fusion (Phase 2.2c)
-- ============================================================================
-- Additive, idempotent. Supports the integration half of Sattar's probabilistic
-- crowdsourcing technique (docs/phases/phase-2.2c-spatiotemporal-fusion.md).
--
-- Three columns, each enabling a specific step of the method:
--
-- 1. asset_observation.sensor_class_probs -- the per-event distribution over
--    classes. This was ALREADY being computed and thrown away: score.py's
--    _class_posteriors builds the full {pothole, crack, not} posterior and
--    score_observation kept only the argmax and P(pothole). The method's §4.5
--    integrates distributions, not scalars, so it needs the vector.
--
-- 2. asset_cluster.class_probs -- the integrated distribution for the cluster.
--    Stored rather than derived because it is the output of a weighted fit over
--    members whose weights depend on the cluster's own spread; recomputing it in
--    a read query would mean re-running the fit per request.
--
-- 3. asset_cluster.bearing_deg -- the mean heading of the member observations.
--    §4.4 makes direction part of cluster identity, so that opposing carriageways
--    of the same road do not merge into one defect. Nullable: clusters formed
--    before this migration have no bearing, and the assignment step treats NULL
--    as "matches any direction" rather than excluding them.
--
-- No backfill of sensor_class_probs. Re-scoring existing observations is a
-- deliberate operator action (set scored_at = NULL and let the fusion job re-run),
-- not something a migration should do to 2728 rows silently.
-- ============================================================================

ALTER TABLE asset_observation
    ADD COLUMN IF NOT EXISTS sensor_class_probs JSONB;

COMMENT ON COLUMN asset_observation.sensor_class_probs IS
    'Full class posterior from the sensor model, e.g. {"pothole":0.7,"crack":0.2,'
    '"not":0.1}. NULL for rows scored before Phase 2.2c. sensor_p_pothole remains '
    'the pothole component, kept for the read path and existing queries.';

ALTER TABLE asset_cluster
    ADD COLUMN IF NOT EXISTS class_probs JSONB;

ALTER TABLE asset_cluster
    ADD COLUMN IF NOT EXISTS bearing_deg DOUBLE PRECISION;

COMMENT ON COLUMN asset_cluster.class_probs IS
    'Integrated class distribution over the cluster members, spatiotemporally '
    'weighted (Phase 2.2c). NULL when the cluster was built by the DBSCAN path.';

COMMENT ON COLUMN asset_cluster.bearing_deg IS
    'Mean heading of member observations, degrees. Used to keep opposing '
    'carriageways separate during cluster assignment. NULL matches any direction.';
