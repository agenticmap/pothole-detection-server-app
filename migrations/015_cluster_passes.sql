-- ============================================================================
-- Migration 015 — corroboration by passes, not just devices
-- ============================================================================
-- Sattar et al. integrate detections "from multiple users AND/OR multiple passes
-- of any road segment", and their own validation was ONE phone driven on five
-- different days "to simulate the data collection model operated by different
-- users". The server only ever counted distinct_devices, so one car over the same
-- defect on three days scored 1 -- identical to that car firing three times in two
-- seconds. On the collected data that left every cluster at distinct_devices = 1
-- and the public read path empty.
--
-- distinct_passes counts contiguous drives (CLUSTER_PASS_GAP_MINUTES between
-- records opens a new one), derived server-side from each device's own timeline.
-- The Android app already records the same notion as a "run" but does not upload
-- it; when it does, this column can be sourced from the wire instead of derived.
--
-- member_span_s is the diagnostic that exposed the problem in the first place: at
-- the measured median 13 m/s, CLUSTER_EPS_M (25 m) is 1.9 seconds of travel, so a
-- cluster spanning ~2 s satisfied min_points without any corroboration at all.
-- Every cluster on pothole_db spanned a median of 2.0 s. It is stored so that fact
-- is visible without re-joining observation_cluster_link.
-- ============================================================================

ALTER TABLE asset_cluster
    ADD COLUMN IF NOT EXISTS distinct_passes INT NOT NULL DEFAULT 0;

ALTER TABLE asset_cluster
    ADD COLUMN IF NOT EXISTS member_span_s DOUBLE PRECISION;

COMMENT ON COLUMN asset_cluster.distinct_passes IS
    'Distinct (device, drive) passes contributing to this cluster. The paper''s '
    'unit of corroboration; distinct_devices is the stricter multi-user variant.';

COMMENT ON COLUMN asset_cluster.member_span_s IS
    'Seconds between the earliest and latest member. A span of a few seconds means '
    'one drive-past, not corroboration.';

-- The read path filters on it, same as distinct_devices.
CREATE INDEX IF NOT EXISTS idx_asset_cluster_passes
    ON asset_cluster (distinct_passes);
