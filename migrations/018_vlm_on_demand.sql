-- ============================================================================
-- Pothole Detection Platform — groundwork for on-demand VLM verification
-- ============================================================================
-- Additive, idempotent. Phase 2.11.
--
-- WHAT THIS IS NOT. There is no endpoint and no operator button yet, deliberately.
-- The first real measurement of a VLM on this corpus returned recall 0.015 -- it said
-- "not a pothole" on 64 of 65 real potholes -- and ~40 s/frame on CPU. A button that
-- hands an operator a confident wrong answer during triage is worse than no button.
-- These two objects are the blockers that had to be removed first, so that when there
-- is a model worth asking, the remaining work is the endpoint and nothing else.
--
-- 1. asset_frame.vlm_verified_at
--
--    phase-2.9-vlm-verification.md sketches the architecture an on-demand path wants:
--    "a vlm_verified_at flag plus a separate job". The column did not exist in any of
--    the 17 previous migrations, so there was no way to record that a frame HAD been
--    verified without re-deriving it from server_detections -- and no way to claim one
--    for verification without racing another worker.
--
--    Deliberately separate from detected_at. Stage 1 and the VLM are different
--    questions asked at different times: a frame can be scored and never verified, and
--    after a model change it can need re-verifying without re-running the detector.
--    Collapsing them is the mistake boxed_at vs boxes_drafted_at exists to prevent one
--    layer up.
--
-- 2. user_quota
--
--    vlm_max_calls_per_run is a PER-INSTANCE counter and an instance is one job run,
--    so a fresh get_detector() per request resets it to zero: N requests would be N
--    uncapped calls against a metered API. The existing limiter cannot be reused as
--    is -- it is keyed on device_id, covers only 'events' and 'frames', and FAILS OPEN
--    by design, which is right for ingestion and exactly wrong for something that
--    costs money per call.
-- ============================================================================

-- ── 1. When a VLM last answered about this frame ────────────────────────────

ALTER TABLE asset_frame
    ADD COLUMN IF NOT EXISTS vlm_verified_at TIMESTAMPTZ;

COMMENT ON COLUMN asset_frame.vlm_verified_at IS
    'When a VLM last returned a verdict for this frame. NULL means never asked, '
    'which is distinct from asked-and-said-no. Separate from detected_at because '
    'Stage 1 and the VLM are different questions asked at different times.';

-- Partial: the queue for "verify this" is the unverified set, and once the corpus is
-- mostly verified a full index would be mostly dead weight. Mirrors the shape of
-- idx_frame_label_unboxed from 017.
CREATE INDEX IF NOT EXISTS idx_asset_frame_unverified
    ON asset_frame (received_at)
    WHERE vlm_verified_at IS NULL;

-- ── 2. A per-user, fail-closed quota ────────────────────────────────────────
--
-- Same sliding-window shape as device_rate_limit (migrations/001) so the accounting
-- is one statement and cannot drift between check and consume. Keyed on user_id
-- rather than device_id because the caller is a staff account, not a phone.

CREATE TABLE IF NOT EXISTS user_quota (
    user_id      TEXT        NOT NULL,
    resource     TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count        INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, resource, window_start)
);

COMMENT ON TABLE user_quota IS
    'Sliding-window quota for metered per-user actions. Unlike device_rate_limit '
    'this FAILS CLOSED: device_rate_limit guards ingestion, where dropping a drive '
    'because the database hiccuped is worse than letting one through, while this '
    'guards outbound paid API calls, where the opposite holds.';

CREATE INDEX IF NOT EXISTS idx_user_quota_window
    ON user_quota (window_start);
