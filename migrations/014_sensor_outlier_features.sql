-- ============================================================================
-- Migration 014 — record which features the outlier gate was fitted on
-- ============================================================================
-- The IsolationForest gate used to be fitted on a hardcoded five-feature set
-- that included ratio, gbar and magnitude — the three features on which
-- potholes separate from everything else by 14-15x. The gate therefore learned
-- "pothole" and reported it as "outlier": on the collected data it flagged 285
-- of 286 pothole-classed observations, and the cluster member gate is
-- `sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`.
--
-- The feature set is now configurable (SENSOR_OUTLIER_FEATURES), which means a
-- model must record what it was fitted on. Scoring a model with a different set
-- feeds sklearn a vector of the wrong width, or — worse — the right width in the
-- wrong order, which fails silently.
--
-- NULL means "fitted before this migration", i.e. the legacy five-feature set.
-- app/sensor_model/store.py resolves NULL to features.LEGACY_OUTLIER_FEATURES
-- rather than to today's default, so an old row still scores as it was fitted.
-- ============================================================================

ALTER TABLE sensor_model
    ADD COLUMN IF NOT EXISTS outlier_features_jsonb JSONB;

COMMENT ON COLUMN sensor_model.outlier_features_jsonb IS
    'Ordered feature names the IsolationForest was fitted on. NULL = the legacy '
    'pre-014 set (ratio, gbar, magnitude, accel_std, speed_mps).';
