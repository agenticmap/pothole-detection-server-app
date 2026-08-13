-- ============================================================================
-- 006: GPS quality on observations
-- ============================================================================
-- Adds horizontal accuracy to asset_observation so degraded fixes (tunnels,
-- urban canyon, cold start) can be filtered out during analysis instead of
-- silently polluting the sensor-model fit. Nullable: the mobile client only
-- began sending accuracy_m at Room schema v4, and the platform may omit it on
-- any individual fix.
--
-- speed_accuracy_mps already exists (001_initial_schema.sql) but was always
-- received as 0.0 until the client stopped hardcoding it.
-- ============================================================================

ALTER TABLE asset_observation
    ADD COLUMN IF NOT EXISTS accuracy_m DOUBLE PRECISION;

COMMENT ON COLUMN asset_observation.accuracy_m IS
    'GPS horizontal accuracy in metres as reported by the device, or NULL if unreported.';
